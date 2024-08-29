#!/usr/bin/env python3
# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

# @nolint
# pyre-ignore-all-errors

import unittest
from argparse import Namespace

import torch
from torchrec.datasets.criteo import DEFAULT_CAT_NAMES, DEFAULT_INT_NAMES
from torchrec.distributed.global_settings import set_propogate_device
from torchrec.distributed.test_utils.test_model import (
    ModelInput,
    TestOverArchRegroupModule,
    TestSparseNN,
)

from torchrec.inference.dlrm_predict import (
    create_training_batch,
    DLRMModelConfig,
    DLRMPredictFactory,
)
from torchrec.inference.modules import (
    assign_weights_to_tbe,
    get_table_to_weights_from_tbe,
    quantize_inference_model,
    shard_quant_model,
)
from torchrec.modules.embedding_configs import EmbeddingBagConfig


class InferenceTest(unittest.TestCase):
    def setUp(self) -> None:
        num_features = 4
        num_weighted_features = 2

        self.tables = [
            EmbeddingBagConfig(
                num_embeddings=(i + 1) * 10,
                embedding_dim=(i + 1) * 4,
                name="table_" + str(i),
                feature_names=["feature_" + str(i)],
            )
            for i in range(num_features)
        ]
        self.weighted_tables = [
            EmbeddingBagConfig(
                num_embeddings=(i + 1) * 10,
                embedding_dim=(i + 1) * 4,
                name="weighted_table_" + str(i),
                feature_names=["weighted_feature_" + str(i)],
            )
            for i in range(num_weighted_features)
        ]

    def test_dlrm_inference_package(self) -> None:
        args = Namespace()
        args.batch_size = 10
        args.num_embedding_features = 26
        args.num_dense_features = len(DEFAULT_INT_NAMES)
        args.dense_arch_layer_sizes = "512,256,64"
        args.over_arch_layer_sizes = "512,512,256,1"
        args.sparse_feature_names = ",".join(DEFAULT_CAT_NAMES)
        args.num_embeddings = 100_000
        args.num_embeddings_per_feature = ",".join(
            [str(args.num_embeddings)] * args.num_embedding_features
        )

        batch = create_training_batch(args)

        model_config = DLRMModelConfig(
            dense_arch_layer_sizes=list(
                map(int, args.dense_arch_layer_sizes.split(","))
            ),
            dense_in_features=args.num_dense_features,
            embedding_dim=64,
            id_list_features_keys=args.sparse_feature_names.split(","),
            num_embeddings_per_feature=list(
                map(int, args.num_embeddings_per_feature.split(","))
            ),
            num_embeddings=args.num_embeddings,
            over_arch_layer_sizes=list(map(int, args.over_arch_layer_sizes.split(","))),
            sample_input=batch,
        )

        # Create torchscript model for inference
        DLRMPredictFactory(model_config).create_predict_module(
            world_size=1, device="cpu"
        )

    def test_regroup_module_inference(self) -> None:
        set_propogate_device(True)
        model = TestSparseNN(
            tables=self.tables,
            weighted_tables=self.weighted_tables,
            num_float_features=10,
            dense_device=torch.device("cpu"),
            sparse_device=torch.device("cpu"),
            over_arch_clazz=TestOverArchRegroupModule,
        )

        model.eval()
        _, local_batch = ModelInput.generate(
            batch_size=16,
            world_size=1,
            num_float_features=10,
            tables=self.tables,
            weighted_tables=self.weighted_tables,
        )

        with torch.inference_mode():
            output = model(local_batch[0])

            # Quantize the model and collect quantized weights
            quantized_model = quantize_inference_model(model)
            quantized_output = quantized_model(local_batch[0])
            table_to_weight = get_table_to_weights_from_tbe(quantized_model)

            # Shard the model, all weights are initialized back to 0, so have to reassign weights
            sharded_quant_model, _ = shard_quant_model(
                quantized_model,
                world_size=2,
                compute_device="cpu",
                sharding_device="cpu",
            )
            assign_weights_to_tbe(quantized_model, table_to_weight)

            sharded_quant_output = sharded_quant_model(local_batch[0])

            self.assertTrue(torch.allclose(output, quantized_output, atol=1e-4))
            self.assertTrue(torch.allclose(output, sharded_quant_output, atol=1e-4))