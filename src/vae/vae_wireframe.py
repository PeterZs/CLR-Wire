import torch
import torch.nn as nn
from torch.nn import Module
from typing import Optional
from torchtyping import TensorType
from typing import Union
from einops import rearrange, repeat, pack

from beartype import beartype
from diffusers.configuration_utils import ConfigMixin, register_to_config
from diffusers.utils.accelerate_utils import apply_forward_hook
from diffusers.models.modeling_utils import ModelMixin
from diffusers.models.autoencoders.vae import DecoderOutput, DiagonalGaussianDistribution

from src.vae.modules import AutoencoderKLOutput, MLP, AttentionLayerFactory, PointEmbed, FocalLoss, ce_loss

class EmbeddingLayerFactory:
    def __init__(
        self, 
        point_embed_dim: int, 
        max_col_diff: int, 
        col_diff_embed_dim: int,
        max_row_diff: int, 
        row_diff_embed_dim: int, 
    ):
        self.point_embed_dim = point_embed_dim
        self.max_col_diff = max_col_diff
        self.col_diff_embed_dim = col_diff_embed_dim
        self.max_row_diff = max_row_diff
        self.row_diff_embed_dim = row_diff_embed_dim

    def create_embeddings(self):
        point_embed = PointEmbed(dim=self.point_embed_dim)
        
        col_diff_embed = nn.Embedding(self.max_col_diff, self.col_diff_embed_dim)
        row_diff_embed = nn.Embedding(self.max_row_diff, self.row_diff_embed_dim)
        return point_embed, col_diff_embed, row_diff_embed


class Encoder1D(Module):
    def __init__(
        self,
        out_channels = 8,
        coor_embed_dim = 128,
        max_col_diff=6,
        max_row_diff=32,
        col_diff_embed_dim = 16,
        row_diff_embed_dim = 32,
        max_curves_num = 128,
        curve_latent_channels = 12,
        curve_latent_embed_dim = 128,
        attn_kwargs: dict = dict(
            dim = 512,
            depth = 4,
            heads = 8,
        ),        
        double_z = True,
        wireframe_latent_num = 64,
    ):
        super().__init__()
      
        self.max_curves_num = max_curves_num
        self.wireframe_latent_num = wireframe_latent_num
        
        embedding_factory = EmbeddingLayerFactory(
            point_embed_dim = coor_embed_dim * 3,
            max_col_diff = max_col_diff,
            col_diff_embed_dim = col_diff_embed_dim,
            max_row_diff = max_row_diff,
            row_diff_embed_dim = row_diff_embed_dim,
        )

        (
            self.point_embed, 
            self.col_diff_embed, 
            self.row_diff_embed
        ) = embedding_factory.create_embeddings()
        
        attn_dim = attn_kwargs['dim']
        
        # latent mu embedding
        
        self.latent_embed = nn.Linear(curve_latent_channels, curve_latent_embed_dim)

        self.enc_learnable_queries = nn.Parameter(torch.randn(wireframe_latent_num, attn_dim))            
        
        # position embedding
        
        self.pos_emb = nn.Parameter(torch.randn(max_curves_num, attn_dim))

        # init feature embedding dim
        
        init_dim = (coor_embed_dim * 6 + col_diff_embed_dim + row_diff_embed_dim + curve_latent_embed_dim)

        self.attn_project_in = nn.Linear(init_dim, attn_dim)

        attn_factory = AttentionLayerFactory()
        self.cross_attn = attn_factory.create_x_transformer_cross_attn_layer(**attn_kwargs)
                    
        out_channels = 2 * out_channels if double_z else out_channels

        self.project_out = nn.Linear(attn_dim, out_channels)
        
        # register buffer
        self.register_buffer('max_col_diff', torch.tensor(max_col_diff))
        self.register_buffer('max_row_diff', torch.tensor(max_row_diff))

    def forward(
        self,
        *,
        xs:                     TensorType['b', 'nv', 6+12, float],
        flag_diffs:             TensorType['b', 'nl', 1+2, int],  # as edges
        return_segment_coords:  bool = False
    ):
        bs = xs.shape[0]
        
        line_coords = xs[...,:6] # bs, nl, 6
        
        points = rearrange(line_coords, 'b nl (nlv d) -> b nl nlv d', nlv = 2)
        points = rearrange(points, 'b nl nlv d -> (b nl nlv) d')

        line_coor_embed = self.point_embed(points) #  (bs, nl, 6, dim_coor_embed)
        line_coor_embed = rearrange(line_coor_embed, '(b nl nlv) d -> b nl nlv d', b=bs, nlv=2)
        line_coor_embed = rearrange(line_coor_embed, 'b nl nlv d -> b nl (nlv d)')

        flag = flag_diffs[..., 0].unsqueeze(-1) # bs, nl, 1
        diffs = flag_diffs[..., 1:] # bs, nl, 2
        
        col_diff = diffs[..., 0]
        row_diff = diffs[..., 1]

        col_diff_embed = self.col_diff_embed(col_diff)
        row_diff_embed = self.row_diff_embed(row_diff)

        curve_latent = xs[..., 6:] # bs, nv, 12
        curve_latent_embed = self.latent_embed(curve_latent) # bs, nv, 128
        
        wire_embed, _ = pack([line_coor_embed, col_diff_embed, row_diff_embed, curve_latent_embed], 'b nl *')

        wire_embed = self.attn_project_in(wire_embed)
        
        wire_embed = self.pos_emb + wire_embed
        
        # multi cross Attention

        # ==================== use learnable ====================
        
        enc_learnable_query = repeat(self.enc_learnable_queries, 'n d -> b n d', b=wire_embed.shape[0])

        # ==================== cross attention ====================
        
        context_padding_mask = rearrange(flag < 0.5, 'b n c -> b (n c)')

        wireframe_latent_embed = self.cross_attn(x=enc_learnable_query, context=wire_embed, context_mask=~context_padding_mask)

        # project out

        wireframe_latent = self.project_out(wireframe_latent_embed)

        if not return_segment_coords:
            return wireframe_latent
        
        return wireframe_latent

class Decoder1D(Module):
    def __init__(
        self, 
        *,
        in_channels: int = 8,
        attn_kwargs: dict = dict(
            dim = 512,
            heads = 8,
            self_depth = 6,
            cross_depth = 2,
        ),
        max_curves_num = 128,
        wireframe_latent_num = 64,
        use_latent_pos_emb: bool = False,
    ):
        super().__init__()
        
        self.max_curves_num = max_curves_num
        self.use_latent_pos_emb = use_latent_pos_emb

        attn_dim = attn_kwargs['dim']

        self.proj_in = nn.Linear(in_channels, attn_dim)

        if self.use_latent_pos_emb:
            self.pos_emb = nn.Parameter(torch.randn(wireframe_latent_num, attn_dim))

        self.dec_learnable_query = nn.Parameter(torch.randn(1 + max_curves_num, attn_dim))
        
        attn_factory = AttentionLayerFactory()
        
        self.self_attn = attn_factory.create_x_transformer_self_attn_layer(
            dim=attn_kwargs['dim'],
            heads=attn_kwargs['heads'], 
            depth=attn_kwargs['self_depth']
        )
        
        self.cross_attn = attn_factory.create_x_transformer_cross_attn_layer(
            dim=attn_kwargs['dim'],
            heads=attn_kwargs['heads'], 
            depth=attn_kwargs['cross_depth']
        )

        self.proj_out = nn.Linear(attn_dim, attn_dim)

    @beartype
    def forward(
        self,
        zs: TensorType['b', 'n', 'd', float],
    ):
        bs = zs.shape[0]
        wireframe_latent = self.proj_in(zs)

        if self.use_latent_pos_emb:
            wireframe_latent = self.pos_emb + wireframe_latent

        # self attn
        
        wireframe_latent = self.self_attn(wireframe_latent)
        
        # cross attn
        
        query_embed = repeat(self.dec_learnable_query, 'n d -> b n d', b=bs)
        
        query_embed = self.cross_attn(query_embed, wireframe_latent)

        # proj out
        
        query_embed = self.proj_out(query_embed)

        return query_embed

class AutoencoderKLWireframe(ModelMixin, ConfigMixin):

    @register_to_config
    def __init__(
        self,
        latent_channels: int = 8,
        max_col_diff=6,
        max_row_diff=32,
        attn_encoder_depth: int = 4,
        attn_decoder_self_depth: int = 6,
        attn_decoder_cross_depth: int = 2,
        attn_dim: int = 512,
        num_heads: int = 8,
        max_curves_num: int = 128,
        wireframe_latent_num: int = 64,
        label_smoothing: float = 0.005,
        cls_loss_weight: float = 1.,
        segment_loss_weight: float = 1.,
        col_diff_loss_weight: float = 1.,
        row_diff_loss_weight: float = 1.,
        curve_latent_loss_weight: float = 1.,
        kl_loss_weight: float = 2e-4,
        curve_latent_embed_dim: int = 256,
        use_mlp_predict: bool = False,
        use_focal_loss: bool = False,
        use_latent_pos_emb: bool = False,
        # **kwargs,
    ):
        super().__init__()
        
        self.max_col_diff = max_col_diff
        self.max_row_diff = max_row_diff
        self.max_curves_num = max_curves_num

        attn_kwargs = dict(
            dim = attn_dim,
            heads = num_heads,
            depth = attn_encoder_depth,
        )

        self.encoder = Encoder1D(
            out_channels=latent_channels,
            attn_kwargs=attn_kwargs,
            max_curves_num=max_curves_num,
            wireframe_latent_num=wireframe_latent_num,
            curve_latent_embed_dim=curve_latent_embed_dim,
        )

        attn_kwargs = dict(
            dim = attn_dim,
            heads = num_heads,
            self_depth = attn_decoder_self_depth,
            cross_depth = attn_decoder_cross_depth,
        )

        self.decoder = Decoder1D(
            in_channels=latent_channels, 
            attn_kwargs=attn_kwargs,
            max_curves_num=max_curves_num,
            use_latent_pos_emb=use_latent_pos_emb,
        )
        
        
        self.use_mlp_predict = use_mlp_predict
        if use_mlp_predict:
            dim = attn_dim
            
            expansion_factor = 1.0
            self.predict_cls = MLP(in_dim=dim, out_dim=max_curves_num, expansion_factor=expansion_factor, dropout=0.1)
            self.predict_diffs = MLP(in_dim=dim, out_dim=max_col_diff+max_row_diff, expansion_factor=expansion_factor, dropout=0.1)
            self.predict_segments = MLP(in_dim=dim, out_dim=6, expansion_factor=expansion_factor)
            self.predict_curve_latent = MLP(in_dim=dim, out_dim=12, expansion_factor=expansion_factor) 
        else:
            out_dim = 6 + 6 + 32 + 12 # num_segments + num_col_diffs + num_row_diffs + num_curve_latent
            self.predict_cls = nn.Linear(attn_dim, max_curves_num)
            self.predict_features = nn.Linear(attn_dim, out_dim)
        

        self.quant_proj = nn.Linear(2 * latent_channels, 2 * latent_channels)
        self.post_quant_proj = nn.Linear(latent_channels, latent_channels)

        # for loss function
        self.mse_loss_fn = torch.nn.MSELoss(reduction='none')
        self.focal_loss = FocalLoss(gamma=2)
        if use_focal_loss:
            self.ce_loss = self.focal_loss
        else:
            self.ce_loss = ce_loss
        
        self.cls_loss_weight = cls_loss_weight
        self.segment_loss_weight = segment_loss_weight
        self.col_diff_loss_weight = col_diff_loss_weight
        self.row_diff_loss_weight = row_diff_loss_weight
        self.curve_latent_loss_weight = curve_latent_loss_weight
        self.kl_loss_weight = kl_loss_weight

        self.pad_id = -1
        
        self.label_smoothing = label_smoothing

        col_diff_labels = torch.linspace(-1, 1, self.max_col_diff)
        col_diff_class_weights = torch.exp(col_diff_labels)
        
        row_diff_labels = torch.linspace(-1, 1, self.max_row_diff)
        row_diff_class_weights = torch.exp(row_diff_labels)

        t = torch.linspace(0, 2, self.max_curves_num)
        col_weights = 1.2 - 0.2 * torch.log(t + 1.7183)


        self.register_buffer('col_diff_class_weights', col_diff_class_weights)
        self.register_buffer('row_diff_class_weights', row_diff_class_weights)
        self.register_buffer('col_weights', col_weights)

    @apply_forward_hook
    def encode(
        self, 
        *,
        xs:                 TensorType['b', 'nl', 6, float],
        flag_diffs:              TensorType['b', 'nl', 2, int],
        return_dict:        bool = True,
        return_segment_coords: bool = True,
    ) -> AutoencoderKLOutput:

        h = self.encoder(
            xs=xs, 
            flag_diffs=flag_diffs, 
        )
        # assert not torch.isnan(h).any(), "h is NaN"

        moments = self.quant_proj(h) 

        moments = rearrange(moments, 'b n d -> b d n')

        # assert not torch.isnan(moments).any(), "moments is NaN"

        posterior = DiagonalGaussianDistribution(moments)

        if not return_dict:
            return (posterior,)


        if not return_segment_coords:
            return AutoencoderKLOutput(latent_dist=posterior)
        
        return AutoencoderKLOutput(
            latent_dist=posterior, 
        )

    def _decode(
        self, 
        *,
        zs:              TensorType['b', 'nl', 'd', float],
        return_dict:    bool = True
    ) -> Union[DecoderOutput, torch.FloatTensor]:

        zs = rearrange(zs, 'b d n -> b n d')
        zs = self.post_quant_proj(zs)
        dec = self.decoder(zs)

        if not return_dict:
            return (dec,)

        return DecoderOutput(sample=dec)

    # @apply_forward_hook
    def decode(
        self, 
        *,
        z:              TensorType['b', 'nl', 'd', float], 
        return_dict:    bool = True,
    ) -> Union[DecoderOutput, torch.FloatTensor]:

        decoded = self._decode(zs=z).sample

        if not return_dict:
            return (decoded,)

        return DecoderOutput(sample=decoded)


    def linear_predict(self, dec):
        num_segments = 6
        num_diffs = num_segments + self.max_col_diff + self.max_row_diff
        
        pred_cls_logits = self.predict_cls(dec[:, 0])
        pred_features_logits = self.predict_features(dec[:, 1:])

        pred_segments = pred_features_logits[..., :num_segments]
        assert pred_segments.shape[-1] == num_segments
        pred_diffs_logits = pred_features_logits[..., num_segments:num_diffs]
        assert pred_diffs_logits.shape[-1] == self.max_col_diff + self.max_row_diff
        pred_curve_latent = pred_features_logits[..., num_diffs:]
        assert pred_curve_latent.shape[-1] == 12
        
        return pred_cls_logits, pred_segments, pred_diffs_logits, pred_curve_latent

    def mlp_predict(self, dec):
        # for multi task
        cls_token = dec[:, 0]
        features_tokens = dec[:, 1:]
        pred_cls_logits = self.predict_cls(cls_token)
        pred_segments = self.predict_segments(features_tokens)
        pred_diffs_logits = self.predict_diffs(features_tokens)
        pred_curve_latent = self.predict_curve_latent(features_tokens)
        
        return pred_cls_logits, pred_segments, pred_diffs_logits, pred_curve_latent

    def loss(
        self, 
        *,
        gt_segment_coords,
        gt_flag_diffs,
        gt_curve_latent,
        xs_mask,
        preds,
    ):
        (
            pred_cls_logits, 
            pred_segments, 
            pred_diffs_logits, 
            pred_curve_latent_mu,
        ) = (
            preds['cls'],
            preds['segments'],
            preds['diffs'],
            preds['curve_latent'],
        )
        
        bs = pred_cls_logits.shape[0]

        cls = gt_flag_diffs[..., 0].sum(dim=-1) - 1
        diffs = gt_flag_diffs[..., 1:]

        # ================== cls ce loss =================

        cls_ce_loss = self.ce_loss(
            pred_cls_logits, 
            cls, 
            reduction='mean',
            label_smoothing=self.label_smoothing, 
            num_classes=self.max_curves_num,
        )
        
        # ================== segment ce loss =================


        segment_mse_loss = self.mse_loss_fn(pred_segments, gt_segment_coords)

        line_mask = repeat(xs_mask, 'b nl -> b nl r', r = 6)
        segment_mse_loss = segment_mse_loss[line_mask].mean()

        # ================== col diff and row diff ce loss =================
        
        rearranged_logits = rearrange(pred_diffs_logits, 'b ... c -> b c (...)')
        
        pred_col_diff_logits, pred_row_diff_logits = rearranged_logits.split([self.max_col_diff, rearranged_logits.shape[1] - self.max_col_diff], dim=1)
        
        col_diff_ce_loss = self.ce_loss(
            pred_col_diff_logits, 
            diffs[..., 0],
            num_classes=self.max_col_diff,
            label_smoothing=self.label_smoothing,
            weight=self.col_diff_class_weights,
            reduction='none',
        )
        
        row_diff_ce_loss = self.ce_loss(
            pred_row_diff_logits, 
            diffs[..., 1],
            num_classes=self.max_row_diff,
            label_smoothing=self.label_smoothing,
            weight=self.row_diff_class_weights,
            reduction='none',
        )
        
                    
        col_weights = repeat(self.col_weights, 'n -> b n', b=bs)
                    
        col_diff_ce_loss = (col_diff_ce_loss * col_weights)[xs_mask].mean()
        row_diff_ce_loss = row_diff_ce_loss[xs_mask].mean()

        # ================== curve latent mse loss =================
        gt_curve_latent_std = torch.clamp(gt_curve_latent[..., 12:], 0., 1.)
        mu_weights = 1.2 - 0.5 * torch.log(gt_curve_latent_std + 1.7183)

        curve_latent_mask = repeat(xs_mask, 'b nl -> b nl r', r = 12)
        curve_latent_loss = (mu_weights * self.mse_loss_fn(pred_curve_latent_mu, gt_curve_latent[..., :12]))[curve_latent_mask].mean()

        return cls_ce_loss, segment_mse_loss, col_diff_ce_loss, row_diff_ce_loss, curve_latent_loss

    def forward(
        self,
        xs: TensorType['b', 'nl', 6+12+12, float],
        flag_diffs: TensorType['b', 'nl', 1+2, int],
        sample_posterior: bool = False,  # True
        return_dict: bool = True,
        generator: Optional[torch.Generator] = None,
        return_loss: bool = False,
        **kwargs,
    ) -> Union[DecoderOutput, torch.FloatTensor]:
        r"""
        Args:
            xs: segment coordinates and curve latent
            sample (`torch.FloatTensor`): Input sample.
            sample_posterior (`bool`, *optional*, defaults to `False`):
                Whether to sample from the posterior.
            return_dict (`bool`, *optional*, defaults to `True`):
                Whether or not to return a [`DecoderOutput`] instead of a plain tuple.
        """

        # prepare masks
        flags = flag_diffs[..., 0]
        xs_mask = flags > 0.5

        # encode and get posterior
        ae_kl_output = self.encode(
            xs=xs[..., :18], # 6 + 12
            flag_diffs=flag_diffs, 
        )

        posterior = ae_kl_output.latent_dist
        segment_coords = xs[..., :6]
        
        # sample_posterior = False
        if sample_posterior:
            z = posterior.sample(generator=generator)
        else:
            z = posterior.mode()
        # decode
        dec = self.decode(z=z).sample

        if self.use_mlp_predict:
            pred_cls_logits, pred_segments, pred_diffs_logits, pred_curve_latent = self.mlp_predict(dec)
        else:
            pred_cls_logits, pred_segments, pred_diffs_logits, pred_curve_latent = self.linear_predict(dec)

        preds = {
            'cls': pred_cls_logits,
            'segments': pred_segments,
            'diffs': pred_diffs_logits,
            'curve_latent': pred_curve_latent,
        }


        if not return_dict:
            return (preds,)

        if return_loss:
            kl_loss = 0.5 * torch.sum(torch.pow(posterior.mean, 2) + posterior.var - 1.0 - posterior.logvar, dim = [1,2]).mean()
            
            if not sample_posterior:
                kl_loss = 0 * kl_loss
            
            (
                cls_ce_loss, 
                segment_mse_loss, 
                col_diff_ce_loss, 
                row_diff_ce_loss,
                curve_latent_loss,
            ) = self.loss(
                gt_segment_coords=segment_coords,
                gt_flag_diffs=flag_diffs,
                gt_curve_latent = xs[..., 6:],
                xs_mask=xs_mask,
                preds=preds,
            )
            
            all_losses = dict(
                cls_ce_loss=cls_ce_loss,
                segment_mse_loss=segment_mse_loss,
                col_diff_ce_loss=col_diff_ce_loss,
                row_diff_ce_loss=row_diff_ce_loss,
                curve_latent_loss=curve_latent_loss,
                kl_loss=kl_loss,
            )
            
            new_kl_loss_weight = self.kl_loss_weight
            
            loss = (self.cls_loss_weight*cls_ce_loss
                + self.segment_loss_weight*segment_mse_loss
                + self.col_diff_loss_weight*col_diff_ce_loss 
                + self.row_diff_loss_weight*row_diff_ce_loss 
                + self.curve_latent_loss_weight*curve_latent_loss
                + new_kl_loss_weight*kl_loss
            )
            
            
            all_losses['mu'] = posterior.mean.abs().mean()
            all_losses['std'] = posterior.std.mean()

            return loss, all_losses
        
        return preds


class AutoencoderKLWireframeFastEncode(ModelMixin, ConfigMixin):
    _supports_gradient_checkpointing = True

    @register_to_config
    def __init__(
        self,
        latent_channels: int = 8,
        max_col_diff=6,
        max_row_diff=32,
        attn_encoder_depth: int = 4,
        attn_dim: int = 512,
        num_heads: int = 8,
        max_curves_num: int = 128,
        wireframe_latent_num: int = 64,
        curve_latent_embed_dim: int = 128,
        **kwargs,
    ):
        super().__init__()
        
        self.max_col_diff = max_col_diff
        self.max_row_diff = max_row_diff
        self.max_curves_num = max_curves_num

        attn_kwargs = dict(
            dim = attn_dim,
            heads = num_heads,
            depth = attn_encoder_depth,
        )

        self.encoder = Encoder1D(
            out_channels=latent_channels,
            attn_kwargs=attn_kwargs,
            max_curves_num=max_curves_num,
            wireframe_latent_num=wireframe_latent_num,
            curve_latent_embed_dim=curve_latent_embed_dim,
        )

        self.quant_proj = nn.Linear(2 * latent_channels, 2 * latent_channels)

    @apply_forward_hook
    def encode(
        self, 
        *,
        xs:                 TensorType['b', 'nl', 6, float],
        flag_diffs:              TensorType['b', 'nl', 2, int],
        return_dict:        bool = True,
    ) -> AutoencoderKLOutput:

        h = self.encoder(
            xs=xs, 
            flag_diffs=flag_diffs, 
        )

        moments = self.quant_proj(h) 

        moments = rearrange(moments, 'b n d -> b d n')

        posterior = DiagonalGaussianDistribution(moments)

        if not return_dict:
            return (posterior,)
    
        return AutoencoderKLOutput(
            latent_dist=posterior, 
        )

    def forward(
        self,
        xs: TensorType['b', 'nl', 6, float],
        flag_diffs: TensorType['b', 'nl', 2, int],
        sample_posterior: bool = False,  # True
        generator: Optional[torch.Generator] = None,
        return_std: bool = False,
        **kwargs,
    ) -> Union[DecoderOutput, torch.FloatTensor]:
        r"""
        Args:
            xs: segment coordinates and curve latent
            sample (`torch.FloatTensor`): Input sample.
            sample_posterior (`bool`, *optional*, defaults to `False`):
                Whether to sample from the posterior.
            return_dict (`bool`, *optional*, defaults to `True`):
                Whether or not to return a [`DecoderOutput`] instead of a plain tuple.
        """

        # encode and get posterior
        ae_kl_output = self.encode(
            xs=xs[..., :18], 
            flag_diffs=flag_diffs, 
        )

        posterior = ae_kl_output.latent_dist

        # if sample_posterior:
        #     z = posterior.sample(generator=generator)
        # else:
        #     z = posterior.mode()
        
        mu = posterior.mode()
        
        if return_std:
            std = posterior.std
            zs = torch.cat([mu, std], dim=1)
            return zs
        
        return mu


class AutoencoderKLWireframeFastDecode(ModelMixin, ConfigMixin):
    _supports_gradient_checkpointing = True

    @register_to_config
    def __init__(
        self,
        latent_channels: int = 8,
        max_col_diff=6,
        max_row_diff=32,
        attn_decoder_self_depth: int = 6,
        attn_decoder_cross_depth: int = 2,
        attn_dim: int = 512,
        num_heads: int = 8,
        max_curves_num: int = 128,
        use_mlp_predict: bool = False,
        use_latent_pos_emb: bool = False,
        **kwargs,
    ):
        super().__init__()
        
        self.max_col_diff = max_col_diff
        self.max_row_diff = max_row_diff
        self.max_curves_num = max_curves_num

        attn_kwargs = dict(
            dim = attn_dim,
            heads = num_heads,
            self_depth = attn_decoder_self_depth,
            cross_depth = attn_decoder_cross_depth,
        )

        self.decoder = Decoder1D(
            in_channels=latent_channels, 
            attn_kwargs=attn_kwargs,
            max_curves_num=max_curves_num,
            use_latent_pos_emb=use_latent_pos_emb,
        )

        self.use_mlp_predict = use_mlp_predict
        if use_mlp_predict:
            dim = attn_dim
            
            expansion_factor = 1.0
            self.predict_cls = MLP(in_dim=dim, out_dim=max_curves_num, expansion_factor=expansion_factor, dropout=0.1)
            self.predict_diffs = MLP(in_dim=dim, out_dim=max_col_diff+max_row_diff, expansion_factor=expansion_factor, dropout=0.1)
            self.predict_segments = MLP(in_dim=dim, out_dim=6, expansion_factor=expansion_factor)
            self.predict_curve_latent = MLP(in_dim=dim, out_dim=12, expansion_factor=expansion_factor) 
        else:
            out_dim = 6 + 6 + 32 + 12 # num_segments + num_col_diffs + num_row_diffs + num_curve_latent
            self.predict_cls = nn.Linear(attn_dim, max_curves_num)
            self.predict_features = nn.Linear(attn_dim, out_dim)
        

        self.post_quant_proj = nn.Linear(latent_channels, latent_channels)

    def _decode(
        self, 
        *,
        zs:              TensorType['b', 'nl', 'd', float],
        return_dict:    bool = True
    ) -> Union[DecoderOutput, torch.FloatTensor]:

        zs = rearrange(zs, 'b d n -> b n d')
        zs = self.post_quant_proj(zs)
        dec = self.decoder(zs)

        if not return_dict:
            return (dec,)

        return DecoderOutput(sample=dec)

    @apply_forward_hook
    def decode(
        self, 
        *,
        zs:              TensorType['b', 'nl', 'd', float], 
        return_dict:    bool = True,
    ) -> Union[DecoderOutput, torch.FloatTensor]:

        decoded = self._decode(zs=zs).sample

        if not return_dict:
            return (decoded,)

        return DecoderOutput(sample=decoded)

    def linear_predict(self, dec):
        num_segments = 6
        num_diffs = num_segments + self.max_col_diff + self.max_row_diff
        
        pred_cls_logits = self.predict_cls(dec[:, 0])
        pred_features_logits = self.predict_features(dec[:, 1:])

        pred_segments = pred_features_logits[..., :num_segments]
        assert pred_segments.shape[-1] == num_segments
        pred_diffs_logits = pred_features_logits[..., num_segments:num_diffs]
        assert pred_diffs_logits.shape[-1] == self.max_col_diff + self.max_row_diff
        pred_curve_latent = pred_features_logits[..., num_diffs:]
        assert pred_curve_latent.shape[-1] == 12
        
        return pred_cls_logits, pred_segments, pred_diffs_logits, pred_curve_latent

    def mlp_predict(self, dec):
        # for multi task
        cls_token = dec[:, 0]
        features_tokens = dec[:, 1:]
        pred_cls_logits = self.predict_cls(cls_token)
        pred_segments = self.predict_segments(features_tokens)
        pred_diffs_logits = self.predict_diffs(features_tokens)
        pred_curve_latent = self.predict_curve_latent(features_tokens)
        
        return pred_cls_logits, pred_segments, pred_diffs_logits, pred_curve_latent


    def forward(
        self,
        zs: TensorType['b', 'nwl', 16, float],
        return_dict: bool = True,
        **kwargs,
    ) -> Union[DecoderOutput, torch.FloatTensor]:
        r"""
        Args:
            xs: segment coordinates and curve latent
            sample (`torch.FloatTensor`): Input sample.
            sample_posterior (`bool`, *optional*, defaults to `False`):
                Whether to sample from the posterior.
            return_dict (`bool`, *optional*, defaults to `True`):
                Whether or not to return a [`DecoderOutput`] instead of a plain tuple.
        """

        zs = rearrange(zs, 'b nwl d -> b d nwl')

        # decode
        dec = self.decode(zs=zs).sample

        if self.use_mlp_predict:
            pred_cls_logits, pred_segments, pred_diffs_logits, pred_curve_latent = self.mlp_predict(dec)
        else:
            pred_cls_logits, pred_segments, pred_diffs_logits, pred_curve_latent = self.linear_predict(dec)

        preds = {
            'cls': pred_cls_logits,
            'segments': pred_segments,
            'diffs': pred_diffs_logits,
            'curve_latent': pred_curve_latent,
        }

        if not return_dict:
            return (dec,)

        return preds