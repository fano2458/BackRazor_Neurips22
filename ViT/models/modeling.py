# coding=utf-8
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import copy
import logging
import math

from os.path import join as pjoin

import torch
import torch.nn as nn
import numpy as np

from torch.nn import CrossEntropyLoss, Dropout, Softmax, Linear, Conv2d, LayerNorm
from torch.nn.modules.utils import _pair
from scipy import ndimage

import ViT.models.configs as configs

from .modeling_resnet import ResNetV2
from custom_functions.custom_layer_norm import LayerNormSparse

from .modeling_new_prune import AttentionActPrune, MlpActPrune

from pdb import set_trace

ATTENTION_Q = "MultiHeadDotProductAttention_1/query"
ATTENTION_K = "MultiHeadDotProductAttention_1/key"
ATTENTION_V = "MultiHeadDotProductAttention_1/value"
ATTENTION_OUT = "MultiHeadDotProductAttention_1/out"
FC_0 = "MlpBlock_3/Dense_0"
FC_1 = "MlpBlock_3/Dense_1"
ATTENTION_NORM = "LayerNorm_0"
MLP_NORM = "LayerNorm_2"


class StochasticDepth(nn.Module):
    def __init__(self, module: torch.nn.Module, p: float = 0.5):
        super().__init__()
        if not 0 < p < 1:
            raise ValueError("Stochastic Depth p has to be between 0 and 1 but got {}".format(p))
        self.module: nn.Module = module
        self.p: float = p
        self._sampler = torch.Tensor(1)

    def forward(self, inputs):
        # print("Type of inputs is ", type(inputs))
        if self.training:
            if self._sampler.uniform_() < self.p:      # Dropping the layer or block
                return inputs
            else:
                outputs = self.module(inputs)
                # print("Type of outputs is ", type(outputs))
                return outputs * (1 - self.p)  # Scaling during training
        else:
            return self.module(inputs)                 # No scaling during inference


def np2th(weights, conv=False):
    """Possibly convert HWIO to OIHW."""
    if conv:
        weights = weights.transpose([3, 2, 0, 1])
    return torch.from_numpy(weights)


def swish(x):
    return x * torch.sigmoid(x)


ACT2FN = {"gelu": torch.nn.functional.gelu, "relu": torch.nn.functional.relu, "swish": swish}


class Attention(nn.Module):
    def __init__(self, config, vis, prune_mode=False, prune_after_softmax=False, n_tokens=1):
        super(Attention, self).__init__()
        self.vis = vis
        self.prune_mode = prune_mode
        self.prune_after_softmax = prune_after_softmax
        self.num_attention_heads = config.transformer["num_heads"]
        self.attention_head_size = int(config.hidden_size / self.num_attention_heads)
        self.all_head_size = self.num_attention_heads * self.attention_head_size

        self.query = Linear(config.hidden_size, self.all_head_size)
        self.key = Linear(config.hidden_size, self.all_head_size)
        self.value = Linear(config.hidden_size, self.all_head_size)

        self.out = Linear(config.hidden_size, config.hidden_size)
        self.attn_dropout = Dropout(config.transformer["attention_dropout_rate"])
        self.proj_dropout = Dropout(config.transformer["attention_dropout_rate"])

        self.n_tokens = n_tokens
        if self.prune_mode:
            self.attention_mask = nn.Parameter(torch.ones(1, self.num_attention_heads,
                                                          n_tokens, n_tokens).bool(), requires_grad=False)
            self.record_attn_mean_var = None

        self.softmax = Softmax(dim=-1)
        self.attention_probs = None
        self.record_attention_probs = False

    def transpose_for_scores(self, x):
        new_x_shape = x.size()[:-1] + (self.num_attention_heads, self.attention_head_size)
        x = x.view(*new_x_shape)
        return x.permute(0, 2, 1, 3)

    def forward(self, hidden_states):
        mixed_query_layer = self.query(hidden_states)
        mixed_key_layer = self.key(hidden_states)
        mixed_value_layer = self.value(hidden_states)

        query_layer = self.transpose_for_scores(mixed_query_layer)
        key_layer = self.transpose_for_scores(mixed_key_layer)
        value_layer = self.transpose_for_scores(mixed_value_layer)
        #print(query_layer.shape)
        attention_scores = torch.matmul(query_layer, key_layer.transpose(-1, -2))
        attention_scores = attention_scores / math.sqrt(self.attention_head_size)

        if self.prune_mode:
            if (not self.prune_after_softmax):
                attention_scores.masked_fill_(~self.attention_mask.detach(), float('-inf'))
                attention_probs = self.softmax(attention_scores)

            if self.prune_after_softmax:
                # print("prune after SM")
                attention_probs_ = self.softmax(attention_scores)
                attention_probs = (~self.attention_mask.detach()).float() * attention_probs_
        else:
            attention_probs = self.softmax(attention_scores)

        if self.record_attention_probs:
            self.attention_probs = attention_probs
        if self.prune_mode and (self.record_attn_mean_var is not None):
            self.record_attn_mean_var.update(attention_probs.detach())

        weights = attention_probs if self.vis else None
        attention_probs = self.attn_dropout(attention_probs)

        context_layer = torch.matmul(attention_probs, value_layer)
        context_layer = context_layer.permute(0, 2, 1, 3).contiguous()
        new_context_layer_shape = context_layer.size()[:-2] + (self.all_head_size,)
        context_layer = context_layer.view(*new_context_layer_shape)
        attention_output = self.out(context_layer)
        attention_output = self.proj_dropout(attention_output)
        return attention_output, weights


class Mlp(nn.Module):
    def __init__(self, config):
        super(Mlp, self).__init__()
        self.fc1 = Linear(config.hidden_size, config.transformer["mlp_dim"])
        self.fc2 = Linear(config.transformer["mlp_dim"], config.hidden_size)
        self.act_fn = ACT2FN["gelu"]
        self.dropout = Dropout(config.transformer["dropout_rate"])

        self._init_weights()

    def _init_weights(self):
        nn.init.xavier_uniform_(self.fc1.weight)
        nn.init.xavier_uniform_(self.fc2.weight)
        nn.init.normal_(self.fc1.bias, std=1e-6)
        nn.init.normal_(self.fc2.bias, std=1e-6)

    def forward(self, x):
        x = self.fc1(x)
        x = self.act_fn(x)
        x = self.dropout(x)
        x = self.fc2(x)
        x = self.dropout(x)
        return x


class Embeddings(nn.Module):
    """Construct the embeddings from patch, position embeddings.
    """
    def __init__(self, config, img_size, in_channels=3):
        super(Embeddings, self).__init__()
        self.hybrid = None
        img_size = _pair(img_size)

        if config.patches.get("grid") is not None:
            grid_size = config.patches["grid"]
            patch_size = (img_size[0] // 16 // grid_size[0], img_size[1] // 16 // grid_size[1])
            n_patches = (img_size[0] // 16) * (img_size[1] // 16)
            self.hybrid = True
        else:
            patch_size = _pair(config.patches["size"])
            n_patches = (img_size[0] // patch_size[0]) * (img_size[1] // patch_size[1])
            self.hybrid = False

        if self.hybrid:
            self.hybrid_model = ResNetV2(block_units=config.resnet.num_layers,
                                         width_factor=config.resnet.width_factor)
            in_channels = self.hybrid_model.width * 16
        self.patch_embeddings = Conv2d(in_channels=in_channels,
                                       out_channels=config.hidden_size,
                                       kernel_size=patch_size,
                                       stride=patch_size)
        self.position_embeddings = nn.Parameter(torch.zeros(1, n_patches+1, config.hidden_size))
        self.cls_token = nn.Parameter(torch.zeros(1, 1, config.hidden_size))

        self.dropout = Dropout(config.transformer["dropout_rate"])

    def forward(self, x):
        B = x.shape[0]
        cls_tokens = self.cls_token.expand(B, -1, -1)

        if self.hybrid:
            x = self.hybrid_model(x)
        x = self.patch_embeddings(x)
        x = x.flatten(2)
        x = x.transpose(-1, -2)
        x = torch.cat((cls_tokens, x), dim=1)

        embeddings = x + self.position_embeddings
        embeddings = self.dropout(embeddings)
        return embeddings


class Block(nn.Module):
    def __init__(self, config, vis, prune_mode=False, prune_after_softmax=False, n_tokens=1,
                 masker=None, new_backrazor=False, layer_drop=False, drop_prob=0.5):
        super(Block, self).__init__()
        self.hidden_size = config.hidden_size

        self.new_backrazor = new_backrazor
        self.layer_drop = layer_drop

        if new_backrazor:
            self.attention_norm = LayerNormSparse(config.hidden_size, eps=1e-6, masker=masker, quantize=config.quantize)
            self.ffn_norm = LayerNormSparse(config.hidden_size, eps=1e-6, masker=masker, quantize=config.quantize)
        else:
            self.attention_norm = LayerNorm(config.hidden_size, eps=1e-6)
            self.ffn_norm = LayerNorm(config.hidden_size, eps=1e-6)

        if new_backrazor and layer_drop:
            self.ffn = StochasticDepth(MlpActPrune(config, masker), p=drop_prob)
        elif new_backrazor:
            self.ffn = MlpActPrune(config, masker)
        else:
            self.ffn = Mlp(config)

        # if new_backrazor and layer_drop:
        #     self.attn = StochasticDepth(AttentionActPrune(config, vis, masker), p=drop_prob)
        if new_backrazor:
            self.attn = AttentionActPrune(config, vis, masker)
        else:
            self.attn = Attention(config, vis, prune_mode, prune_after_softmax, n_tokens)

    def forward(self, x):
        h = x
        x = self.attention_norm(x)
        x = self.attn(x) # removed weights
        x = x + h

        # print("attn output {}".format(x.mean(-1).mean(-1)))

        h = x
        x = self.ffn_norm(x)
        x = self.ffn(x)
        x = x + h

        # print("mlp output {}".format(x.mean(-1).mean(-1)))

        return x

    def load_from(self, weights, n_block):
        ROOT = f"Transformer/encoderblock_{n_block}"
        with torch.no_grad():
            query_weight = np2th(weights[pjoin(ROOT, ATTENTION_Q, "kernel")]).view(self.hidden_size, self.hidden_size).t()
            key_weight = np2th(weights[pjoin(ROOT, ATTENTION_K, "kernel")]).view(self.hidden_size, self.hidden_size).t()
            value_weight = np2th(weights[pjoin(ROOT, ATTENTION_V, "kernel")]).view(self.hidden_size, self.hidden_size).t()
            out_weight = np2th(weights[pjoin(ROOT, ATTENTION_OUT, "kernel")]).view(self.hidden_size, self.hidden_size).t()

            query_bias = np2th(weights[pjoin(ROOT, ATTENTION_Q, "bias")]).view(-1)
            key_bias = np2th(weights[pjoin(ROOT, ATTENTION_K, "bias")]).view(-1)
            value_bias = np2th(weights[pjoin(ROOT, ATTENTION_V, "bias")]).view(-1)
            out_bias = np2th(weights[pjoin(ROOT, ATTENTION_OUT, "bias")]).view(-1)

            self.attn.module.query.weight.copy_(query_weight)
            self.attn.module.key.weight.copy_(key_weight)
            self.attn.module.value.weight.copy_(value_weight)
            self.attn.module.out.weight.copy_(out_weight)
            self.attn.module.query.bias.copy_(query_bias)
            self.attn.module.key.bias.copy_(key_bias)
            self.attn.module.value.bias.copy_(value_bias)
            self.attn.module.out.bias.copy_(out_bias)

            mlp_weight_0 = np2th(weights[pjoin(ROOT, FC_0, "kernel")]).t()
            mlp_weight_1 = np2th(weights[pjoin(ROOT, FC_1, "kernel")]).t()
            mlp_bias_0 = np2th(weights[pjoin(ROOT, FC_0, "bias")]).t()
            mlp_bias_1 = np2th(weights[pjoin(ROOT, FC_1, "bias")]).t()

            self.ffn.module.fc1.weight.copy_(mlp_weight_0)
            self.ffn.module.fc2.weight.copy_(mlp_weight_1)
            self.ffn.module.fc1.bias.copy_(mlp_bias_0)
            self.ffn.module.fc2.bias.copy_(mlp_bias_1)

            self.attention_norm.weight.copy_(np2th(weights[pjoin(ROOT, ATTENTION_NORM, "scale")]))
            self.attention_norm.bias.copy_(np2th(weights[pjoin(ROOT, ATTENTION_NORM, "bias")]))
            self.ffn_norm.weight.copy_(np2th(weights[pjoin(ROOT, MLP_NORM, "scale")]))
            self.ffn_norm.bias.copy_(np2th(weights[pjoin(ROOT, MLP_NORM, "bias")]))


class Encoder(nn.Module):
    def __init__(self, config, vis, prune_mode=False, prune_after_softmax=False, n_tokens=1, **block_kwargs):
        super(Encoder, self).__init__()
        self.vis = vis
        self.prune_mode = prune_mode
        self.layer = nn.ModuleList()
        self.encoder_norm = LayerNorm(config.hidden_size, eps=1e-6)
        for _ in range(config.transformer["num_layers"]):
            layer = Block(config, vis, prune_mode, prune_after_softmax, n_tokens, **block_kwargs)
            self.layer.append(copy.deepcopy(layer))

    def forward(self, hidden_states):
        attn_weights = []
        for layer_block in self.layer:
            hidden_states = layer_block(hidden_states) # removed , weights
            if self.vis:
                weights = None
                attn_weights.append(weights)
        encoded = self.encoder_norm(hidden_states)
        return encoded #, attn_weights


class Transformer(nn.Module):
    def __init__(self, config, img_size, vis, prune_mode=False, prune_after_softmax=False, quantize=False, half=True, **kwargs):
        super(Transformer, self).__init__()
        self.embeddings = Embeddings(config, img_size=img_size)
        config.quantize = quantize
        config.half = half
        self.encoder = Encoder(config, vis, prune_mode, prune_after_softmax=prune_after_softmax,
                               n_tokens=self.embeddings.position_embeddings.shape[1], **kwargs)

    def forward(self, input_ids):
        # print("input_ids is {}".format(input_ids.mean(-1).mean(-1)))
        embedding_output = self.embeddings(input_ids)
        # print("self.embeddings output {}".format(embedding_output.mean(-1).mean(-1)))
        encoded = self.encoder(embedding_output) # removed , attn_weights
        return encoded # , attn_weights


class VisionTransformer(nn.Module):
    def __init__(self, config, img_size=224, num_classes=21843, zero_head=False, vis=False,
                 prune_mode=False, prune_after_softmax=False, **kwargs):
        super(VisionTransformer, self).__init__()
        self.num_classes = num_classes
        self.zero_head = zero_head
        self.classifier = config.classifier
        self.prune_mode = prune_mode
        self.prune_after_softmax = prune_after_softmax

        self.transformer = Transformer(config, img_size, vis, prune_mode, prune_after_softmax, **kwargs)
        self.head = Linear(config.hidden_size, num_classes)

    def forward(self, x, labels=None, return_encoded_feature=False):
        x = self.transformer(x) # removed , attn_weights
        if return_encoded_feature:
            return x

        logits = self.head(x[:, 0])

        if labels is not None:
            loss_fct = CrossEntropyLoss()
            loss = loss_fct(logits.view(-1, self.num_classes), labels.view(-1))
            return loss
        else:
            return logits # , attn_weights

    def load_from(self, weights):
        with torch.no_grad():
            if self.zero_head:
                nn.init.zeros_(self.head.weight)
                nn.init.zeros_(self.head.bias)
            else:
                self.head.weight.copy_(np2th(weights["head/kernel"]).t())
                self.head.bias.copy_(np2th(weights["head/bias"]).t())

            self.transformer.embeddings.patch_embeddings.weight.copy_(np2th(weights["embedding/kernel"], conv=True))
            self.transformer.embeddings.patch_embeddings.bias.copy_(np2th(weights["embedding/bias"]))
            self.transformer.embeddings.cls_token.copy_(np2th(weights["cls"]))
            self.transformer.encoder.encoder_norm.weight.copy_(np2th(weights["Transformer/encoder_norm/scale"]))
            self.transformer.encoder.encoder_norm.bias.copy_(np2th(weights["Transformer/encoder_norm/bias"]))

            posemb = np2th(weights["Transformer/posembed_input/pos_embedding"])
            posemb_new = self.transformer.embeddings.position_embeddings
            if posemb.size() == posemb_new.size():
                self.transformer.embeddings.position_embeddings.copy_(posemb)
            else:
                print("load_pretrained: resized variant: %s to %s" % (posemb.size(), posemb_new.size()))
                ntok_new = posemb_new.size(1)

                if self.classifier == "token":
                    posemb_tok, posemb_grid = posemb[:, :1], posemb[0, 1:]
                    ntok_new -= 1
                else:
                    posemb_tok, posemb_grid = posemb[:, :0], posemb[0]

                gs_old = int(np.sqrt(len(posemb_grid)))
                gs_new = int(np.sqrt(ntok_new))
                print('load_pretrained: grid-size from %s to %s' % (gs_old, gs_new))
                posemb_grid = posemb_grid.reshape(gs_old, gs_old, -1)

                zoom = (gs_new / gs_old, gs_new / gs_old, 1)
                posemb_grid = ndimage.zoom(posemb_grid, zoom, order=1)
                posemb_grid = posemb_grid.reshape(1, gs_new * gs_new, -1)
                posemb = np.concatenate([posemb_tok, posemb_grid], axis=1)
                self.transformer.embeddings.position_embeddings.copy_(np2th(posemb))

            for bname, block in self.transformer.encoder.named_children():
                for uname, unit in block.named_children():
                    unit.load_from(weights, n_block=uname)

            if self.transformer.embeddings.hybrid:
                self.transformer.embeddings.hybrid_model.root.conv.weight.copy_(np2th(weights["conv_root/kernel"], conv=True))
                gn_weight = np2th(weights["gn_root/scale"]).view(-1)
                gn_bias = np2th(weights["gn_root/bias"]).view(-1)
                self.transformer.embeddings.hybrid_model.root.gn.weight.copy_(gn_weight)
                self.transformer.embeddings.hybrid_model.root.gn.bias.copy_(gn_bias)

                for bname, block in self.transformer.embeddings.hybrid_model.body.named_children():
                    for uname, unit in block.named_children():
                        unit.load_from(weights, n_block=bname, n_unit=uname)


CONFIGS = {
    'ViT-B_16': configs.get_b16_config(),
    'ViT-Ti_16': configs.get_ti16_config(),
    'ViT-B_32': configs.get_b32_config(),
    'ViT-L_16': configs.get_l16_config(),
    'ViT-L_32': configs.get_l32_config(),
    'ViT-H_14': configs.get_h14_config(),
    'R50-ViT-B_16': configs.get_r50_b16_config(),
    'testing': configs.get_testing(),
}
