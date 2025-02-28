from collections import OrderedDict

from typing import List, Optional, Tuple, Union

import torch
import torch.utils.checkpoint
from torch import nn

from .gnn import GOFADecoderLayer, GOFAGatedDecoderLayer
from transformers import MistralConfig, GenerationMixin
from transformers.models.mistral.modeling_mistral import MistralPreTrainedModel, MistralRMSNorm, MistralModel
from transformers.cache_utils import Cache, DynamicCache
from transformers.utils import (
    LossKwargs, logging, )
from transformers.modeling_outputs import (
    BaseModelOutputWithPast,
    CausalLMOutputWithPast, )
from transformers.processing_utils import Unpack
from transformers.modeling_flash_attention_utils import FlashAttentionKwargs

logger = logging.get_logger(__name__)

_CHECKPOINT_FOR_DOC = "mistralai/Mistral-7B-v0.1"
_CONFIG_FOR_DOC = "MistralConfig"

class GOFAMistralModel(MistralModel):
    """
    Transformer decoder consisting of *config.num_hidden_layers* layers. Each layer is a [`MistralDecoderLayer`]

    Args:
        config: MistralConfig
    """

    def __init__(self, config: MistralConfig, gofa_config):
        super().__init__(config)
        self.gofa_config = gofa_config

        self.g_layers = nn.ModuleList([GOFAGatedDecoderLayer(gofa_config, layer_idx=i) for i in range(gofa_config.num_layers)])

        self.post_init()


    def forward(
        self,
        input_ids: torch.LongTensor = None,
        attention_mask: Optional[torch.Tensor] = None,
        position_ids: Optional[torch.LongTensor] = None,
        past_key_values: Optional[Cache] = None,
        inputs_embeds: Optional[torch.FloatTensor] = None,
        use_cache: Optional[bool] = None,
        output_attentions: Optional[bool] = None,
        output_hidden_states: Optional[bool] = None,
        return_dict: Optional[bool] = None,
        cache_position: Optional[torch.LongTensor] = None,
        graph=None,
        mem_mask=None,
        partial_grad=None,
        map_node=None,
        **flash_attn_kwargs: Unpack[FlashAttentionKwargs],
    ) -> Union[Tuple, BaseModelOutputWithPast]:

        # Copied from Huggingface Mistral implementation.

        output_attentions = output_attentions if output_attentions is not None else self.config.output_attentions
        output_hidden_states = (
            output_hidden_states if output_hidden_states is not None else self.config.output_hidden_states
        )
        use_cache = use_cache if use_cache is not None else self.config.use_cache
        return_dict = return_dict if return_dict is not None else self.config.use_return_dict

        if input_ids is not None:
            raise ValueError("You cannot specify input_ids for GOFA, please construct input embeddings manually")

        if (input_ids is None) ^ (inputs_embeds is not None):
            raise ValueError("You must specify exactly one of input_ids or inputs_embeds")

        if self.gradient_checkpointing and self.training and use_cache:
            logger.warning_once(
                "`use_cache=True` is incompatible with gradient checkpointing. Setting `use_cache=False`."
            )
            use_cache = False

        if inputs_embeds is None:
            inputs_embeds = self.embed_tokens(input_ids)

        if use_cache and past_key_values is None:
            past_key_values = DynamicCache()

        if cache_position is None:
            past_seen_tokens = past_key_values.get_seq_length() if past_key_values is not None else 0
            cache_position = torch.arange(
                past_seen_tokens, past_seen_tokens + inputs_embeds.shape[1], device=inputs_embeds.device
            )

        if position_ids is None:
            position_ids = cache_position.unsqueeze(0)
        causal_mask = self._update_causal_mask(
            attention_mask, inputs_embeds, cache_position, past_key_values, output_attentions
        )

        hidden_states = inputs_embeds

        # create position embeddings to be shared across the decoder layers
        position_embeddings = self.rotary_emb(hidden_states, position_ids)

        # decoder layers
        all_hidden_states = () if output_hidden_states else None
        all_self_attns = () if output_attentions else None

        ##########################################################
        #             Key model implementation of GOFA           #
        ##########################################################

        cur_node_size = graph.num_node_feat if graph is not None else 0

        for i, decoder_layer in enumerate(self.layers[: self.config.num_hidden_layers]):
            if output_hidden_states:
                all_hidden_states += (hidden_states,)
            g_layer_idx = i - (self.config.num_hidden_layers - self.gofa_config.num_layers)
            if g_layer_idx >= 0 and graph is not None:
                if g_layer_idx == 0 and map_node:
                    hidden_states = torch.cat(
                        [hidden_states[:cur_node_size][graph.node_map], hidden_states[cur_node_size:]], dim=0)
                    mem_mask = torch.cat([mem_mask[:cur_node_size][graph.node_map], mem_mask[cur_node_size:]], dim=0)
                    causal_mask = torch.cat(
                        [causal_mask[:cur_node_size][graph.node_map], causal_mask[cur_node_size:]], dim=0)
                    cur_node_size = len(graph.node_map)
                mem_repr = hidden_states[mem_mask].view(hidden_states.size()[0], self.gofa_config.mem_token, -1)
                gnn_input = mem_repr[:cur_node_size]
                gnn_edge_input = mem_repr[cur_node_size:][graph.edge_map]

                output = self.g_layers[g_layer_idx](gnn_input, graph.edge_index, gnn_edge_input)
                output = torch.cat([output, mem_repr[cur_node_size:]], dim=0)
                gnn_output = torch.zeros_like(hidden_states, dtype=output.dtype)
                gnn_output[mem_mask] = output.view(-1, output.size()[-1])
                hidden_states = hidden_states * torch.logical_not(mem_mask).unsqueeze(2) + gnn_output
                hidden_states = hidden_states.to(self.gofa_config.llama_dtype)
            if g_layer_idx < 0 and partial_grad:
                with torch.no_grad():
                    layer_outputs = self.llm_forward(decoder_layer, hidden_states, causal_mask, position_ids, past_key_values, output_attentions, use_cache, cache_position, position_embeddings, flash_attn_kwargs)
            else:
                layer_outputs = self.llm_forward(decoder_layer, hidden_states, causal_mask, position_ids,
                                                 past_key_values, output_attentions, use_cache, cache_position,
                                                 position_embeddings, flash_attn_kwargs)

            hidden_states = layer_outputs[0]

            if output_attentions:
                all_self_attns += (layer_outputs[1],)

        hidden_states = self.norm(hidden_states)

        # add hidden states from the last decoder layer
        if output_hidden_states:
            all_hidden_states += (hidden_states,)

        output = BaseModelOutputWithPast(
            last_hidden_state=hidden_states,
            past_key_values=past_key_values if use_cache else None,
            hidden_states=all_hidden_states,
            attentions=all_self_attns,
        )
        return output if return_dict else output.to_tuple()

    def llm_forward(self, decoder_layer, hidden_states, causal_mask, position_ids, past_key_values, output_attentions, use_cache, cache_position, position_embeddings, flash_attn_kwargs):
        if self.gradient_checkpointing and self.training:
            layer_outputs = self._gradient_checkpointing_func(decoder_layer.__call__, hidden_states, causal_mask,
                position_ids, past_key_values, output_attentions, use_cache, cache_position, position_embeddings, )
        else:
            layer_outputs = decoder_layer(hidden_states, attention_mask=causal_mask, position_ids=position_ids,
                past_key_value=past_key_values, output_attentions=output_attentions, use_cache=use_cache,
                cache_position=cache_position, position_embeddings=position_embeddings, **flash_attn_kwargs, )
        return layer_outputs



class LLMGraphCombiner(torch.nn.Module):
    def __init__(self, init_theta=0.0):
        super().__init__()
        self.theta = nn.Parameter(torch.tensor([init_theta]))

    def forward(self, target_feat, additional_feat, val_mask=None):
        alpha = self.theta.sigmoid()
        if val_mask is None:
            return target_feat * alpha + additional_feat*(1-alpha)
        # print(alpha)
        # print((target_feat[val_mask]**2).sum(dim=-1).mean())
        # print((additional_feat ** 2).sum(dim=-1).mean())
        output = torch.zeros_like(target_feat, dtype=additional_feat.dtype)
        output[val_mask] = additional_feat.view(-1, additional_feat.size()[-1]) * (1-alpha)

        val_multiplier = torch.zeros_like(target_feat)
        val_multiplier[torch.logical_not(val_mask)] = 1
        val_multiplier[val_mask] = alpha

        return val_multiplier * target_feat + output


class GOFAMistralParallelModel(MistralModel):
    """
    Transformer decoder consisting of *config.num_hidden_layers* layers. Each layer is a [`MistralDecoderLayer`]

    Args:
        config: MistralConfig
    """

    def __init__(self, config: MistralConfig, gofa_config):
        super().__init__(config)
        self.gofa_config = gofa_config

        self.g_layers = nn.ModuleList()
        self.g_layers.append(nn.ModuleList([GOFADecoderLayer(gofa_config, i) for i in range(gofa_config.num_layers)]))
        self.g_layers.append(nn.ModuleList([LLMGraphCombiner() for _ in range(gofa_config.num_layers)]))
        self.g_layers.append(nn.ModuleList(
            [MistralRMSNorm(config.hidden_size, eps=config.rms_norm_eps) for _ in range(gofa_config.num_layers)]))
        self.g_layers.append(MistralRMSNorm(config.hidden_size, eps=config.rms_norm_eps))

    def align_weight(self):
        n_layers = len(self.layers)
        inactive_layers = n_layers - len(self.g_layers[0])
        partial_state_dict = OrderedDict()
        source_dict = self.layers.state_dict()
        for layer_name in source_dict:
            name_split = layer_name.split(".")
            layer_ind = int(name_split[0])
            if layer_ind >= inactive_layers:
                name_split[0] = str(layer_ind - inactive_layers)
                if name_split[2] in ["v_proj", "q_proj", "k_proj", "o_proj"]:
                    name_split[2] = "g"+name_split[2]
                partial_state_dict[".".join(name_split)] = source_dict[layer_name]

        self.g_layers[0].load_state_dict(partial_state_dict)

    def forward(
        self,
        input_ids: torch.LongTensor = None,
        attention_mask: Optional[torch.Tensor] = None,
        position_ids: Optional[torch.LongTensor] = None,
        past_key_values: Optional[Cache] = None,
        inputs_embeds: Optional[torch.FloatTensor] = None,
        use_cache: Optional[bool] = None,
        output_attentions: Optional[bool] = None,
        output_hidden_states: Optional[bool] = None,
        return_dict: Optional[bool] = None,
        cache_position: Optional[torch.LongTensor] = None,
        graph=None,
        mem_mask=None,
        partial_grad=None,
        map_node=None,
        **flash_attn_kwargs: Unpack[FlashAttentionKwargs],
    ) -> Union[Tuple, BaseModelOutputWithPast]:
        output_attentions = output_attentions if output_attentions is not None else self.config.output_attentions
        output_hidden_states = (
            output_hidden_states if output_hidden_states is not None else self.config.output_hidden_states
        )
        use_cache = use_cache if use_cache is not None else self.config.use_cache
        return_dict = return_dict if return_dict is not None else self.config.use_return_dict

        if input_ids is not None:
            raise ValueError("You cannot specify input_ids for GOFA, please construct input embeddings manually")

        if (input_ids is None) ^ (inputs_embeds is not None):
            raise ValueError("You must specify exactly one of input_ids or inputs_embeds")

        if self.gradient_checkpointing and self.training and use_cache:
            logger.warning_once(
                "`use_cache=True` is incompatible with gradient checkpointing. Setting `use_cache=False`."
            )
            use_cache = False

        if inputs_embeds is None:
            inputs_embeds = self.embed_tokens(input_ids)

        if use_cache and past_key_values is None:
            past_key_values = DynamicCache()

        if cache_position is None:
            past_seen_tokens = past_key_values.get_seq_length() if past_key_values is not None else 0
            cache_position = torch.arange(
                past_seen_tokens, past_seen_tokens + inputs_embeds.shape[1], device=inputs_embeds.device
            )

        if position_ids is None:
            position_ids = cache_position.unsqueeze(0)

        causal_mask = self._update_causal_mask(
            attention_mask, inputs_embeds, cache_position, past_key_values, output_attentions
        )

        hidden_states = inputs_embeds

        # create position embeddings to be shared across the decoder layers
        position_embeddings = self.rotary_emb(hidden_states, position_ids)

        # decoder layers
        all_hidden_states = () if output_hidden_states else None
        all_self_attns = () if output_attentions else None

        cur_node_size = graph.num_node_feat if graph is not None else 0

        for i, decoder_layer in enumerate(self.layers[: self.config.num_hidden_layers]):
            if output_hidden_states:
                all_hidden_states += (hidden_states,)
            g_layer_idx = i - (self.config.num_hidden_layers - self.gofa_config.num_layers)
            if g_layer_idx == 0 and map_node:
                hidden_states = torch.cat(
                    [hidden_states[:cur_node_size][graph.node_map], hidden_states[cur_node_size:]], dim=0)
                mem_mask = torch.cat([mem_mask[:cur_node_size][graph.node_map], mem_mask[cur_node_size:]], dim=0)
                causal_mask = torch.cat(
                    [causal_mask[:cur_node_size][graph.node_map], causal_mask[cur_node_size:]], dim=0)
                cur_node_size = len(graph.node_map)
            if g_layer_idx >= 0 and graph is not None:
                mem_repr = hidden_states[mem_mask].view(hidden_states.size()[0], self.gofa_config.mem_token, -1)
                gnn_input = mem_repr[:cur_node_size]
                gnn_edge_input = mem_repr[cur_node_size:][graph.edge_map]

                output = self.g_layers[0][g_layer_idx](gnn_input, graph.edge_index, gnn_edge_input)
                output = self.g_layers[2][g_layer_idx](output)
                graph_output = torch.cat([output, mem_repr[cur_node_size:]],
                                         dim=0)  # gnn_output = torch.zeros_like(hidden_states, dtype=output.dtype)  # gnn_output[mem_mask] = output.view(-1, output.size()[-1])  # hidden_states = hidden_states * torch.logical_not(mem_mask).unsqueeze(2) + gnn_output
            else:
                graph_output = None

            if g_layer_idx < 0 and partial_grad:
                with torch.no_grad():
                    layer_outputs = self.llm_forward(decoder_layer, hidden_states, causal_mask, position_ids, past_key_values, output_attentions, use_cache, cache_position, position_embeddings, flash_attn_kwargs)
            else:
                layer_outputs = self.llm_forward(decoder_layer, hidden_states, causal_mask, position_ids,
                                                 past_key_values, output_attentions, use_cache, cache_position,
                                                 position_embeddings, flash_attn_kwargs)

            hidden_states = layer_outputs[0]
            if graph_output is not None:
                hidden_states = self.g_layers[1][g_layer_idx](hidden_states, graph_output, mem_mask)

            if output_attentions:
                all_self_attns += (layer_outputs[1],)

        if graph is not None:
            hidden_states = self.g_layers[3](hidden_states)
        else:
            hidden_states = self.norm(hidden_states)

        # add hidden states from the last decoder layer
        if output_hidden_states:
            all_hidden_states += (hidden_states,)

        output = BaseModelOutputWithPast(
            last_hidden_state=hidden_states,
            past_key_values=past_key_values if use_cache else None,
            hidden_states=all_hidden_states,
            attentions=all_self_attns,
        )
        return output if return_dict else output.to_tuple()

    def llm_forward(self, decoder_layer, hidden_states, causal_mask, position_ids, past_key_values, output_attentions, use_cache, cache_position, position_embeddings, flash_attn_kwargs):
        if self.gradient_checkpointing and self.training:
            layer_outputs = self._gradient_checkpointing_func(decoder_layer.__call__, hidden_states, causal_mask,
                position_ids, past_key_values, output_attentions, use_cache, cache_position, position_embeddings, )
        else:
            layer_outputs = decoder_layer(hidden_states, attention_mask=causal_mask, position_ids=position_ids,
                past_key_value=past_key_values, output_attentions=output_attentions, use_cache=use_cache,
                cache_position=cache_position, position_embeddings=position_embeddings, **flash_attn_kwargs, )
        return layer_outputs

class KwargsForCausalLM(FlashAttentionKwargs, LossKwargs): ...

class GOFAMistralForCausalLM(MistralPreTrainedModel, GenerationMixin):
    _tied_weights_keys = ["lm_head.weight"]
    _keep_in_fp32_modules = ["g_layers"]
    _tp_plan = {"lm_head": "colwise_rep"}
    _pp_plan = {"lm_head": (["hidden_states"], ["logits"])}

    def __init__(self, config, gofa_config):
        super().__init__(config)
        if gofa_config.fuse_type == "parallel":
            self.model = GOFAMistralParallelModel(config, gofa_config)
        else:
            self.model = GOFAMistralModel(config, gofa_config)
        self.vocab_size = config.vocab_size
        self.lm_head = nn.Linear(config.hidden_size, config.vocab_size, bias=False)

        # Initialize weights and apply final processing
        self.post_init()

    def get_input_embeddings(self):
        return self.model.embed_tokens

    def set_input_embeddings(self, value):
        self.model.embed_tokens = value

    def get_output_embeddings(self):
        return self.lm_head

    def set_output_embeddings(self, new_embeddings):
        self.lm_head = new_embeddings

    def set_decoder(self, decoder):
        self.model = decoder

    def get_decoder(self):
        return self.model

    def forward(
        self,
        input_ids: torch.LongTensor = None,
        attention_mask: Optional[torch.Tensor] = None,
        position_ids: Optional[torch.LongTensor] = None,
        past_key_values: Optional[Union[Cache, List[torch.FloatTensor]]] = None,
        inputs_embeds: Optional[torch.FloatTensor] = None,
        labels: Optional[torch.LongTensor] = None,
        use_cache: Optional[bool] = None,
        output_attentions: Optional[bool] = None,
        output_hidden_states: Optional[bool] = None,
        return_dict: Optional[bool] = None,
        cache_position: Optional[torch.LongTensor] = None,
        logits_to_keep: Union[int, torch.Tensor] = 0, graph=None, mem_mask=None, partial_grad=None, map_node=None,
        **kwargs: Unpack[KwargsForCausalLM],
    ) -> Union[Tuple, CausalLMOutputWithPast]:

        output_attentions = output_attentions if output_attentions is not None else self.config.output_attentions
        output_hidden_states = (
            output_hidden_states if output_hidden_states is not None else self.config.output_hidden_states
        )
        return_dict = return_dict if return_dict is not None else self.config.use_return_dict

        # decoder outputs consists of (dec_features, layer_state, dec_hidden, dec_attn)
        outputs = self.model(
            input_ids=input_ids,
            attention_mask=attention_mask,
            position_ids=position_ids,
            past_key_values=past_key_values,
            inputs_embeds=inputs_embeds,
            use_cache=use_cache,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=return_dict,
            cache_position=cache_position, graph=graph, mem_mask=mem_mask, partial_grad=partial_grad, map_node=map_node,
            **kwargs,
        )

        hidden_states = outputs[0]
        # Only compute necessary logits, and do not upcast them to float if we are not computing the loss
        slice_indices = slice(-logits_to_keep, None) if isinstance(logits_to_keep, int) else logits_to_keep
        logits = self.lm_head(hidden_states[:, slice_indices, :])

        loss = None
        if labels is not None:
            loss = self.loss_function(logits=logits, labels=labels, vocab_size=self.config.vocab_size, **kwargs)

        if not return_dict:
            output = (logits,) + outputs[1:]
            return (loss,) + output if loss is not None else output

        return CausalLMOutputWithPast(
            loss=loss,
            logits=logits,
            past_key_values=outputs.past_key_values,
            hidden_states=outputs.hidden_states,
            attentions=outputs.attentions,
        )

    def get_base_model(self):
        return self
