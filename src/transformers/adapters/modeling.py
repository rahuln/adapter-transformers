import math

import torch
from torch import nn
import torch.nn.functional as F

from .configuration import AdapterFusionConfig


class Activation_Function_Class(nn.Module):
    """
    Implementation of various activation function.
    """

    def __init__(self, hidden_act):

        if hidden_act.lower() == "relu":
            self.f = nn.functional.relu
        elif hidden_act.lower() == "tanh":
            self.f = torch.tanh
        elif hidden_act.lower() == "swish":

            def swish(x):
                return x * torch.sigmoid(x)

            self.f = swish
        elif hidden_act.lower() == "gelu":

            def gelu_new(x):
                """
                Implementation of the gelu activation function currently in Google Bert repo (identical to OpenAI GPT).
                Also see https://arxiv.org/abs/1606.08415
                """
                return 0.5 * x * (1 + torch.tanh(math.sqrt(2 / math.pi) * (x + 0.044715 * torch.pow(x, 3))))

            self.f = gelu_new
        elif hidden_act.lower() == "gelu_orig":
            self.f = nn.functional.gelu
        elif hidden_act.lower() == "leakyrelu":
            self.f = nn.functional.leaky_relu

        super().__init__()

    def forward(self, x):
        return self.f(x)


# Single Adapter


class Adapter(nn.Module):
    """
    Implementation of a single Adapter block.
    """

    def __init__(
        self,
        input_size,
        down_sample=None,
        non_linearity="relu",
        init_bert_weights=True,
        add_layer_norm_before=True,
        add_layer_norm_after=False,
        residual_before_ln=True,
    ):
        super().__init__()

        self.input_size = input_size
        self.add_layer_norm_before = add_layer_norm_before
        self.add_layer_norm_after = add_layer_norm_after
        self.residual_before_ln = residual_before_ln

        # list for all modules of the adapter, passed into nn.Sequential()
        seq_list = []

        # If we want to have a layer norm on input, we add it to seq_list
        if self.add_layer_norm_before:
            self.adapter_norm_before = nn.LayerNorm(self.input_size)
            seq_list.append(self.adapter_norm_before)

        # if a downsample size is not passed, we just half the size of the original input
        self.down_sample = down_sample
        if down_sample is None:
            self.down_sample = self.input_size // 2

        # ensure that the down sample size is at least 1
        if self.down_sample < 1:
            self.down_sample = 1

        # Linear down projection of the input
        seq_list.append(nn.Linear(self.input_size, self.down_sample))

        # select non-linearity
        self.non_linearity = Activation_Function_Class(non_linearity.lower())

        seq_list.append(self.non_linearity)

        # sequential adapter, first downproject, then non-linearity then upsample. In the forward pass we include the
        # residual connection
        self.adapter_down = nn.Sequential(*seq_list)

        # Up projection to input size
        self.adapter_up = nn.Linear(self.down_sample, self.input_size)

        # If we want to have a layer norm on output, we apply it later after a separate residual connection
        # This means that we learn a new output layer norm, which replaces another layer norm learned in the bert layer
        if self.add_layer_norm_after:
            self.adapter_norm_after = nn.LayerNorm(self.input_size)

        # if we want to initialize with the bert strategy then this function is called for all the linear layers
        if init_bert_weights:
            self.adapter_down.apply(self.init_bert_weights)
            self.adapter_up.apply(self.init_bert_weights)

    def forward(self, x, residual_input):  # , residual_input=None):
        down = self.adapter_down(x)

        up = self.adapter_up(down)

        output = up

        # apply residual connection before layer norm if configured in this way
        if self.residual_before_ln:
            output = output + residual_input

        # apply layer norm if available
        if self.add_layer_norm_after:
            output = self.adapter_norm_after(output)

        # if residual should be applied after layer norm, apply it here
        if not self.residual_before_ln:
            output = output + residual_input

        return output, down, up

    # This is copied from the BertPreTrainedModel class to make this a self containing class.
    @staticmethod
    def init_bert_weights(module):
        """Initialize the weights."""
        if isinstance(module, (nn.Linear, nn.Embedding)):
            # std defaults to 0.02, this might need to be changed
            module.weight.data.normal_(mean=0.0, std=0.02)
        elif isinstance(module, nn.LayerNorm):
            module.bias.data.zero_()
            module.weight.data.fill_(1.0)
        if isinstance(module, nn.Linear) and module.bias is not None:
            module.bias.data.zero_()


# Adapter Fusion


class BertFusion(nn.Module):
    """
    Implementation of an AdapterFusion block.
    """

    def __init__(
        self,
        config: AdapterFusionConfig,
        dense_size,
        attention_probs_dropout_prob,
    ):
        super(BertFusion, self).__init__()
        # if config.hidden_size % config.num_attention_heads != 0:
        #     raise ValueError(
        #         "The hidden size (%d) is not a multiple of the number of attention "
        #         "heads (%d)" % (config.hidden_size, config.num_attention_heads))
        self.config = config

        self.dense_size = dense_size
        self.dropout = nn.Dropout(attention_probs_dropout_prob)

        if not self.config["query"] and not self.config["key"] and not self.config["value"]:
            self.dense = nn.Linear(self.dense_size, 1)

        if self.config["query"]:
            self.query = nn.Linear(self.dense_size, self.dense_size)
            self.query.apply(Adapter.init_bert_weights)

        if self.config["key"]:
            self.key = nn.Linear(self.dense_size, self.dense_size)
            self.key.apply(Adapter.init_bert_weights)

        if self.config["value"]:
            self.value = nn.Linear(self.dense_size, self.dense_size, bias=False)
            self.value.apply(Adapter.init_bert_weights)
            if self.config["value_initialized"]:
                self.value.weight.data = (torch.zeros(self.dense_size, self.dense_size) + 0.000001).fill_diagonal_(1.0)

        if self.config["temperature"]:
            self.T = 50.0
        else:
            self.T = 1.0
        self.reduction = self.T / 1000.0

    def forward(self, query, key, value, residual):

        if self.config["residual_before"]:
            value += residual[:, :, None, :].repeat(1, 1, value.size(2), 1)

        if self.config["query"]:
            query_layer = self.query(query)
        else:
            query_layer = query

        if self.config["key"]:
            key_layer = self.key(key)
        else:
            key_layer = key

        if self.config["value"] and self.config["value_before_softmax"]:
            # key/value have dims => batch, toks, number-of-adapters, feats
            value_layer = self.value(value)
        else:
            value_layer = value

        # Take the dot product between "query" and "key" to get the raw attention scores.
        attention_scores = torch.squeeze(torch.matmul(query_layer.unsqueeze(2), key_layer.transpose(-2, -1)), dim=2)

        attention_scores = self.dropout(attention_scores)

        # Normalize the attention scores to probabilities.
        attention_probs = nn.Softmax(dim=-1)(attention_scores / self.T)
        self.T = max(self.T - self.reduction, 1.0)

        if not self.training:
            self.recent_attention = attention_probs.detach().cpu().numpy()

        context_layer = torch.squeeze(torch.matmul(attention_probs.unsqueeze(2), value_layer), dim=2)

        if self.config["value"] and not self.config["value_before_softmax"]:
            # key/value have dims => batch, toks, number-of-adapters, feats
            context_layer = self.value(context_layer)
        else:
            context_layer = context_layer

        if not self.config["residual_before"]:
            context_layer += residual

        return context_layer


class BertSwitchFusion(nn.Module):
    """
    Implementation of an AdapterFusion block with per-example conditional
    compute across adapters.
    """

    def __init__(
        self,
        config: AdapterFusionConfig,
        dense_size,
    ):
        super(BertSwitchFusion, self).__init__()
        self.config = config
        self.dense_size = dense_size

    def forward(self, inputs, adapter_outputs, adapter_outputs_copy, residual,
        adapter_indices=None, **kwargs):
        # adapter_outputs have dims => batch, toks, number-of-adapters, feats
        bsz, seqlen, num_adapters, _ = adapter_outputs.size()

        if self.config["residual_before"]:
            adapter_outputs += residual[:, :, None, :].repeat(1, 1, num_adapters, 1)

        # use adapter indices to select adapter output per example in batch
        outputs = adapter_outputs[torch.arange(bsz), :, adapter_indices, :]
        # # Old implementation, matrix multiplication instead of indexing
        # adapter_weights = F.one_hot(adapter_indices, num_classes=num_adapters).float()
        # adapter_weights = adapter_weights.unsqueeze(1).unsqueeze(3).repeat(1, seqlen, 1, 1).transpose(-2, -1)
        # outputs = torch.matmul(adapter_weights, adapter_outputs).squeeze(2)

        if not self.config["residual_before"]:
            outputs += residual

        return outputs


class BertWeightedAverageFusion(nn.Module):
    # Implementation of an AdapterFusion block that computes a weighted average
    # of outputs across adapters.

    def __init__(
        self,
        config: AdapterFusionConfig,
        dense_size,
        num_adapters,
        weights_in_kwargs=False,
    ):
        super(BertWeightedAverageFusion, self).__init__()
        self.config = config
        self.dense_size = dense_size
        self.num_adapters = num_adapters
        self.weights_in_kwargs = weights_in_kwargs
        if not self.weights_in_kwargs:
            self.fusion_weights = torch.nn.Parameter(torch.ones(num_adapters))

    def forward(self, inputs, adapter_outputs, adapter_outputs_copy, residual,
        **kwargs):
        # adapter_outputs have dims => batch, toks, number-of-adapters, feats

        if self.config["residual_before"]:
            adapter_outputs += residual[:, :, None, :].repeat(1, 1, num_adapters, 1)

        # convert unnormalized weights to weights, add dimensions to weights
        # parameter, multiply with adapter outputs, and average across the
        # adapters dimension
        if self.weights_in_kwargs:  # assume weights have batch size dimension
            weights = kwargs["adapter_weights"][:, None, :, None]
        else:
            weights = torch.softmax(fusion_weights, dim=0)[None, None, :, None]
        outputs = weights * adapter_outputs
        outputs = torch.sum(outputs, dim=2)

        if not self.config["residual_before"]:
            outputs += residual

        return outputs


class BertGatedFusion(nn.Module):
    # Implementation of an AdapterFusion block that uses a gating function to
    # compute weights for a weighted average of adapter outputs

    def __init__(
        self,
        config: AdapterFusionConfig,
        dense_size,
        num_adapters,
        mean=False,
    ):
        super(BertGatedFusion, self).__init__()
        self.config = config
        self.dense_size = dense_size
        self.num_adapters = num_adapters
        self.mean = mean
        self.gating_fn = torch.nn.Linear(self.dense_size, self.num_adapters)

    def forward(self, inputs, adapter_outputs, adapter_outputs_copy, residual,
        **kwargs):
        # adapter_outputs have dims => batch, toks, number-of-adapters, feats

        if self.config["residual_before"]:
            adapter_outputs += residual[:, :, None, :].repeat(1, 1, self.num_adapters, 1)

        # apply gating function to compute weights, use weights to compute
        # weighted average of adapter outputs
        if self.mean:
            # average inputs over sequence before applying gating function
            seq = adapter_outputs.size(1)
            avg_inputs = inputs.mean(dim=1)
            weights = torch.softmax(self.gating_fn(avg_inputs), dim=-1)
            weights = weights.unsqueeze(1).unsqueeze(2).repeat(1, seq, 1, 1)
            outputs = torch.matmul(weights, adapter_outputs).squeeze(2)
        else:
            weights = torch.softmax(self.gating_fn(inputs), dim=-1).unsqueeze(2)
            outputs = torch.matmul(weights, adapter_outputs).squeeze(2)

        if not self.config["residual_before"]:
            outputs += residual

        return outputs


# Invertible Adapters


def get_subnet_constructor(non_linearity, reduction_factor):
    def subnet(dims_in, dims_out):
        return nn.Sequential(
            nn.Linear(dims_in, dims_in // reduction_factor),
            Activation_Function_Class(non_linearity),
            nn.Linear(dims_in // reduction_factor, dims_out),
        )

    return subnet


class NICECouplingBlock(nn.Module):
    """Coupling Block following the NICE design."""

    def __init__(self, dims_in, dims_c=[], non_linearity="relu", reduction_factor=2):
        super().__init__()

        channels = dims_in[0][0]
        self.split_len1 = channels // 2
        self.split_len2 = channels - channels // 2

        assert all(
            [dims_c[i][1:] == dims_in[0][1:] for i in range(len(dims_c))]
        ), "Dimensions of input and one or more conditions don't agree."
        self.conditional = len(dims_c) > 0
        condition_length = sum([dims_c[i][0] for i in range(len(dims_c))])

        subnet_constructor = get_subnet_constructor(non_linearity, reduction_factor)
        self.F = subnet_constructor(self.split_len2 + condition_length, self.split_len1)
        self.G = subnet_constructor(self.split_len1 + condition_length, self.split_len2)

    def forward(self, x, c=[], rev=False):
        x1, x2 = (x[:, :, : self.split_len1], x[:, :, self.split_len1 :])
        if not rev:
            x2_c = torch.cat([x2, *c], 1) if self.conditional else x2
            y1 = x1 + self.F(x2_c)
            y1_c = torch.cat([y1, *c], 1) if self.conditional else y1
            y2 = x2 + self.G(y1_c)
        else:
            x1_c = torch.cat([x1, *c], 1) if self.conditional else x1
            y2 = x2 - self.G(x1_c)
            y2_c = torch.cat([y2, *c], 1) if self.conditional else y2
            y1 = x1 - self.F(y2_c)

        return torch.cat((y1, y2), -1)

    def jacobian(self, x, rev=False):
        return 0

    def output_dims(self, input_dims):
        assert len(input_dims) == 1, "Can only use 1 input"
        return input_dims


class GLOWCouplingBlock(nn.Module):
    """
    Coupling Block following the GLOW design. The only difference to the RealNVP coupling blocks, is the fact that it
    uses a single subnetwork to jointly predict [s_i, t_i], instead of two separate subnetworks. This reduces
    computational cost and speeds up learning. clamp: Soft clamping for the multiplicative component. The amplification
    or attenuation of each input dimension can be at most ±exp(clamp).
    """

    def __init__(self, dims_in, dims_c=[], non_linearity="relu", reduction_factor=2, clamp=5.0):
        super().__init__()

        channels = dims_in[0][0]
        self.ndims = len(dims_in[0])
        self.split_len1 = channels // 2
        self.split_len2 = channels - channels // 2

        self.clamp = clamp
        self.max_s = math.exp(clamp)
        self.min_s = math.exp(-clamp)

        assert all(
            [tuple(dims_c[i][1:]) == tuple(dims_in[0][1:]) for i in range(len(dims_c))]
        ), f"Dimensions of input and one or more conditions don't agree: {dims_c} vs {dims_in}."
        self.conditional = len(dims_c) > 0
        condition_length = sum([dims_c[i][0] for i in range(len(dims_c))])

        subnet_constructor = get_subnet_constructor(non_linearity, reduction_factor)
        self.s1 = subnet_constructor(self.split_len1 + condition_length, self.split_len2 * 2)
        self.s2 = subnet_constructor(self.split_len2 + condition_length, self.split_len1 * 2)

    def e(self, s):
        return torch.exp(self.clamp * 0.636 * torch.atan(s / self.clamp))

    def log_e(self, s):
        return self.clamp * 0.636 * torch.atan(s / self.clamp)

    def forward(self, x, c=[], rev=False):
        x1, x2 = (x[:, :, : self.split_len1], x[:, :, self.split_len1 :])

        if not rev:
            s2, t2 = x1.clone(), x2.clone()
            y1 = self.e(s2) * x1 + t2

            r1 = self.s1(torch.cat([y1, *c], 1) if self.conditional else y1)
            s1, t1 = r1[:, : self.split_len2], r1[:, self.split_len2 :]
            y2 = self.e(s1) * x2 + t1
            self.last_jac = torch.sum(self.log_e(s1), dim=tuple(range(1, self.ndims + 1))) + torch.sum(
                self.log_e(s2), dim=tuple(range(1, self.ndims + 1))
            )

        else:  # names of x and y are swapped!
            r1 = self.s1(torch.cat([x1, *c], 1) if self.conditional else x1)
            s1, t1 = r1[:, : self.split_len2], r1[:, self.split_len2 :]
            y2 = (x2 - t1) / self.e(s1)

            r2 = self.s2(torch.cat([y2, *c], 1) if self.conditional else y2)
            s2, t2 = r2[:, : self.split_len1], r2[:, self.split_len1 :]
            y1 = (x1 - t2) / self.e(s2)
            self.last_jac = -torch.sum(self.log_e(s1), dim=tuple(range(1, self.ndims + 1))) - torch.sum(
                self.log_e(s2), dim=tuple(range(1, self.ndims + 1))
            )

        return [torch.cat((y1, y2), 1)]

    def jacobian(self, x, c=[], rev=False):
        return self.last_jac

    def output_dims(self, input_dims):
        return input_dims
