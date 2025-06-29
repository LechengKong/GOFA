# ICAE that supports multi span concat
import types

from transformers import AutoTokenizer
import torch.nn as nn
from typing import Optional
from peft import (get_peft_model, LoraConfig)
import math
from modules.gofa.gofa_modeling import GOFAMistralForCausalLM
import torch


class MistralICAE(torch.nn.Module):
    """
    Modified from ICAE (https://github.com/getao/icae). Create lora for encoder and decoder (if requested). The forward
    function is not used in the GOFA project.
    """
    def __init__(self, model_args, training_args, gofa_config):
        super().__init__()
        self.model_args = model_args
        self.training_args = training_args
        self.model_name = model_args.model_name_or_path
        self.icae = GOFAMistralForCausalLM.from_pretrained(self.model_name, gofa_config,
                                                            torch_dtype=torch.float16 if training_args.bf16 is False
                                                            else torch.bfloat16,
                                                            use_flash_attention_2=False, resume_download=False)
        if gofa_config.fuse_type == "parallel":
            self.icae.model.align_weight()

        self.vocab_size = self.icae.config.vocab_size + 1  # [PAD] token
        self.pad_token_id = self.vocab_size - 1
        self.mean_compression_rate = training_args.mean_compression_rate

        # tunable
        self.mem_size = self.training_args.fixed_mem_size
        self.vocab_size_with_mem = self.vocab_size + self.mem_size  # so, the mem tokens are in the range [self.vocab_size, self.vocab_size + self.mem_size)

        # special tokens in addition to mem and length tokens
        self.ae_token_id = self.vocab_size_with_mem + 0
        self.lm_token_id = self.vocab_size_with_mem + 1
        self.ft_token_id = self.vocab_size_with_mem + 2

        self.icae.resize_token_embeddings(self.vocab_size_with_mem + 3)

        # special tokens for Llama-2/Mistral tokenizer
        self.bos_id = 1
        self.eos_id = 2

        self.dim = self.icae.config.hidden_size

        lora_config = self.create_lora_config()
        if self.model_args.dec_lora:
            dec_lora_config = self.create_dec_lora_config()
            self.icae = get_peft_model(self.icae, dec_lora_config, adapter_name="default")
            self.icae.add_adapter("encadapt", lora_config)
            self.icae.set_adapter("default")
        else:
            self.icae = get_peft_model(self.icae, lora_config, adapter_name="encadapt")
            self.icae.set_adapter("encadapt")
        for name, param in self.icae.named_parameters():
            if "g_layers" in name:
                param.requires_grad = True

        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

        self.memory_token_embed = nn.Embedding(self.mem_size + 3, self.dim, padding_idx=None)
        self.loss_fct = nn.CrossEntropyLoss(ignore_index=-100)
        self.tokenizer = AutoTokenizer.from_pretrained(self.model_name, use_fast=False)
        self.left_tokenizer = AutoTokenizer.from_pretrained(self.model_name, use_fast=False)
        self.left_tokenizer.padding_side = "left"
        self.append_sequence = torch.arange(self.vocab_size, self.vocab_size + self.mem_size, dtype=torch.long,
                                            device=device).unsqueeze(0)  # mem tokens

    def compute_num_segments(self, total_length):
        assert total_length > 0
        num_segments = math.ceil(total_length / (self.mem_size * self.mean_compression_rate))
        return num_segments

    def forward(self, input_ids: torch.LongTensor = None, prompt_answer_ids: torch.LongTensor = None,
            labels: Optional[torch.LongTensor] = None, ):
        # encoder part
        batch_size = input_ids.size(0)
        total_length = input_ids.size(1)
        num_segments = self.compute_num_segments(total_length)
        segment_length = math.ceil(total_length / num_segments)

        prompt_answer_embs = self.icae.get_base_model().model.embed_tokens(prompt_answer_ids)
        max_compressed_length = num_segments * self.mem_size
        compress_outputs = torch.zeros((max_compressed_length, self.dim)).to(prompt_answer_embs)

        for segment_idx in range(num_segments):
            start_idx = segment_idx * segment_length
            end_idx = min((segment_idx + 1) * segment_length, total_length)
            segment_input_ids = input_ids[:, start_idx:end_idx]
            segment_input_ids = torch.cat([segment_input_ids, self.append_sequence], dim=1)
            mem_flag = segment_input_ids >= self.vocab_size

            segment_input_embedding = self.icae.get_base_model().model.embed_tokens(segment_input_ids)
            segment_input_embedding[mem_flag] = self.memory_token_embed(
                segment_input_ids[mem_flag] - self.vocab_size).to(segment_input_embedding)

            # compress the current segment
            segment_compress_outputs = self.icae(inputs_embeds=segment_input_embedding, output_hidden_states=True)
            segment_compress_outputs = segment_compress_outputs.hidden_states[-1]

            # collect memory tokens
            compress_outputs[segment_idx * self.mem_size: self.mem_size * (segment_idx + 1)] = segment_compress_outputs[
                mem_flag]

            del segment_input_ids, segment_input_embedding
            torch.cuda.empty_cache()

        # decoder part
        decoder_mem_flag = (prompt_answer_ids >= self.vocab_size) & (
                prompt_answer_ids < self.vocab_size + self.mem_size)  # only mem tokens

        prompt_answer_embs[decoder_mem_flag] = compress_outputs  # replace memory slots
        special_prompt = prompt_answer_ids >= self.vocab_size_with_mem
        prompt_answer_embs[special_prompt] = self.memory_token_embed(
            prompt_answer_ids[special_prompt] - self.vocab_size).to(
            prompt_answer_embs)  # replace special token's embedding from self.memory_token_embed

        if self.training:  # has an independent se.f.decoder
            decoder_outputs = self.decoder(inputs_embeds=prompt_answer_embs, output_hidden_states=True)
        else:
            with self.icae.disable_adapter():  # no independent decoder; use self.icae
                decoder_outputs = self.icae(inputs_embeds=prompt_answer_embs, output_hidden_states=True)

        logits = decoder_outputs.logits
        effective_logits = logits[:, :-1, :].reshape(-1, logits.size(-1))
        target_ids = labels[:, 1:].reshape(-1)
        loss = self.loss_fct(effective_logits, target_ids)
        return {"loss": loss, "logits": logits}

    def tokens_to_embeddings(self, token_ids):  # input_tokens can be either normal tokens and special tokens
        embeddings = self.icae.get_base_model().model.embed_tokens(token_ids)
        special_flags = token_ids >= self.vocab_size
        embeddings[special_flags] = self.memory_token_embed(token_ids[special_flags] - self.vocab_size).to(
            embeddings)  # replace special token's embedding from self.memory_token_embed
        return embeddings

    def _compress(self,
            input_ids: torch.LongTensor = None):  # for inference; compress a fixed length of input into memory slots

        batch_size = input_ids.size(0)
        total_length = input_ids.size(1)
        num_segments = self.compute_num_segments(total_length)
        segment_length = math.ceil(total_length / num_segments)

        max_compressed_length = num_segments * self.mem_size
        compress_outputs = torch.zeros((max_compressed_length, self.dim))

        for segment_idx in range(num_segments):
            start_idx = segment_idx * segment_length
            end_idx = min((segment_idx + 1) * segment_length, total_length)
            segment_input_ids = input_ids[:, start_idx:end_idx]
            segment_input_ids = torch.cat([segment_input_ids, self.append_sequence], dim=1)
            mem_flag = segment_input_ids >= self.vocab_size

            segment_input_embedding = self.icae.get_base_model().model.embed_tokens(segment_input_ids)
            segment_input_embedding[mem_flag] = self.memory_token_embed(
                segment_input_ids[mem_flag] - self.vocab_size).to(segment_input_embedding)

            # compress the current segment
            segment_compress_outputs = self.icae(inputs_embeds=segment_input_embedding, output_hidden_states=True)
            segment_compress_outputs = segment_compress_outputs.hidden_states[-1]

            # collect memory tokens
            compress_outputs[segment_idx * self.mem_size: self.mem_size * (segment_idx + 1)] = segment_compress_outputs[
                mem_flag]

            del segment_input_ids, segment_input_embedding
            torch.cuda.empty_cache()

        return compress_outputs

    def create_lora_config(self):
        lora_config = LoraConfig(

            r=512,

            lora_alpha=32,

            lora_dropout=self.model_args.lora_dropout,

            bias="none",

            task_type="CAUSAL_LM"

        )
        return lora_config

    def create_dec_lora_config(self):
        lora_config = LoraConfig(

            r=32,

            lora_alpha=32,

            lora_dropout=0.05,

            bias="none",

            task_type="CAUSAL_LM"

        )
        return lora_config

    def merge_lora(self):
        self.icae = self.icae.merge_and_unload()

        def re_self(self):
            return self

        def null_func(self):
            return None

        self.icae.get_base_model = types.MethodType(re_self, self.icae)
        self.icae.enable_adapter_layers = types.MethodType(null_func, self.icae)
        self.icae.disable_adapter_layers = types.MethodType(null_func, self.icae)
