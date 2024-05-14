from typing import Iterable, List

import numpy as np
import torch.nn as nn

from .anchor_head_template import AnchorHeadTemplate


class AnchorHeadSingle(AnchorHeadTemplate):
    def __init__(
        self,
        model_cfg,
        input_channels: int,
        num_class: List[int],
        class_names: List[List[str]],
        grid_size: Iterable[int],
        point_cloud_range: Iterable[int],
        predict_boxes_when_training=True,
        **kwargs,
    ):
        super().__init__(
            model_cfg=model_cfg, num_class=num_class, class_names=class_names, grid_size=grid_size, point_cloud_range=point_cloud_range,
            predict_boxes_when_training=predict_boxes_when_training
        )

        self.num_anchors_per_location = sum(self.num_anchors_per_location)

        self.conv_cls = nn.Conv2d(
            input_channels, self.num_anchors_per_location * self.num_class[0],
            kernel_size=1
        )
        self.conv_box = nn.Conv2d(
            input_channels, self.num_anchors_per_location * self.box_coder.code_size,
            kernel_size=1
        )

        self.conv_type_cls = nn.Conv2d(
            input_channels,
            self.num_anchors_per_location * self.num_class[1],
            kernel_size=1,
        )

        if self.model_cfg.get('USE_DIRECTION_CLASSIFIER', None) is not None:
            self.conv_dir_cls = nn.Conv2d(
                input_channels,
                self.num_anchors_per_location * self.model_cfg.NUM_DIR_BINS,
                kernel_size=1
            )
        else:
            self.conv_dir_cls = None
        self.init_weights()

    def init_weights(self):
        pi = 0.01
        nn.init.constant_(self.conv_cls.bias, -np.log((1 - pi) / pi))
        nn.init.constant_(self.conv_type_cls.bias, -np.log((1 - pi) / pi))
        nn.init.normal_(self.conv_box.weight, mean=0, std=0.001)

    def forward(self, data_dict):
        spatial_features_2d = data_dict['spatial_features_2d']

        cls_preds = self.conv_cls(spatial_features_2d)
        box_preds = self.conv_box(spatial_features_2d)
        cls_type_preds = self.conv_type_cls(spatial_features_2d)

        cls_preds = cls_preds.permute(0, 2, 3, 1).contiguous()  # [N, H, W, C]
        box_preds = box_preds.permute(0, 2, 3, 1).contiguous()  # [N, H, W, C]
        cls_type_preds = cls_type_preds.permute(0, 2, 3, 1).contiguous()

        self.forward_ret_dict['cls_preds'] = cls_preds
        self.forward_ret_dict['box_preds'] = box_preds
        self.forward_ret_dict['cls_type_preds'] = cls_type_preds

        if self.conv_dir_cls is not None:
            dir_cls_preds = self.conv_dir_cls(spatial_features_2d)
            dir_cls_preds = dir_cls_preds.permute(0, 2, 3, 1).contiguous()
            self.forward_ret_dict['dir_cls_preds'] = dir_cls_preds
        else:
            dir_cls_preds = None

        if self.training:
            # get the labels for all anchor boxes. targets_dict will contain the following
            # keys: box_cls_labels, box_reg_targets, reg_weights
            targets_dict = self.assign_targets(
                gt_boxes=data_dict['gt_boxes'],
            )
            self.forward_ret_dict.update(targets_dict)

        if not self.training or self.predict_boxes_when_training:
            batch_cls_preds, batch_box_preds = self.generate_predicted_boxes(
                batch_size=data_dict['batch_size'],
                cls_preds=cls_preds, box_preds=box_preds, dir_cls_preds=dir_cls_preds
            )
            batch_cls_type_preds, _ = self.generate_predicted_boxes(
                batch_size=data_dict['batch_size'],
                cls_preds=cls_type_preds, box_preds=box_preds, dir_cls_preds=None,
            )
            data_dict['batch_cls_preds'] = batch_cls_preds
            data_dict['batch_box_preds'] = batch_box_preds
            data_dict['batch_cls_type_preds'] = batch_cls_type_preds
            data_dict['cls_preds_normalized'] = False

        return data_dict

    def get_cls_layer_loss(self):
        # classification loss head 0
        cls_preds = self.forward_ret_dict["cls_preds"]
        box_cls_labels = self.forward_ret_dict["box_cls_labels"]
        one_hot_targets = self._get_one_hot_box_labels(
            box_cls_labels=box_cls_labels,
            n_labels=self.num_class[0],
            dtype=cls_preds.dtype,
        )
        cls_weights = self._get_cls_weights(box_cls_labels)
        batch_size = int(cls_preds.shape[0])
        cls_preds = cls_preds.view(batch_size, -1, self.num_class[0])
        cls_fdi_loss = self._get_weighted_loss(
            cls_preds, one_hot_targets, cls_weights, batch_size
        )

        # classification loss head 1
        cls_type_preds = self.forward_ret_dict.get("cls_type_preds", None)
        box_cls_type_labels = self.forward_ret_dict.get("box_cls_type_labels", None)
        one_hot_type_targets = self._get_one_hot_box_labels(
            box_cls_labels=box_cls_type_labels,
            n_labels=self.num_class[1],
            dtype=cls_type_preds.dtype,
        )
        cls_type_weights = self._get_cls_weights(box_cls_type_labels)
        cls_type_preds = cls_type_preds.view(batch_size, -1, self.num_class[1])
        cls_type_loss = self._get_weighted_loss(
            cls_type_preds, one_hot_type_targets, cls_type_weights, batch_size
        )

        # combine losses
        cls_loss = cls_fdi_loss + cls_type_loss
        tb_dict = {
            'rpn_loss_type_cls': cls_type_loss.item(),
            'rpn_loss_fdi_cls': cls_fdi_loss.item(),
            'rpn_loss_cls': cls_loss.item()
        }
        return cls_loss, tb_dict
