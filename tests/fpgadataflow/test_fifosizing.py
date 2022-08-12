# Copyright (c) 2022 Xilinx, Inc.
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
# * Redistributions of source code must retain the above copyright notice, this
#   list of conditions and the following disclaimer.
#
# * Redistributions in binary form must reproduce the above copyright notice,
#   this list of conditions and the following disclaimer in the documentation
#   and/or other materials provided with the distribution.
#
# * Neither the name of Xilinx nor the names of its
#   contributors may be used to endorse or promote products derived from
#   this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
# DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
# FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
# DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
# SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
# CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
# OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
# OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

import pkg_resources as pk

import pytest

import json
import numpy as np
import shutil
from qonnx.custom_op.registry import getCustomOp
from qonnx.transformation.general import GiveUniqueNodeNames

import finn.builder.build_dataflow as build
import finn.builder.build_dataflow_config as build_cfg
from finn.analysis.fpgadataflow.dataflow_performance import dataflow_performance
from finn.transformation.fpgadataflow.derive_characteristic import DeriveCharacteristic
from finn.transformation.fpgadataflow.hlssynth_ip import HLSSynthIP
from finn.transformation.fpgadataflow.insert_dwc import InsertDWC
from finn.transformation.fpgadataflow.prepare_ip import PrepareIP
from finn.transformation.fpgadataflow.prepare_rtlsim import PrepareRTLSim
from finn.util.basic import make_build_dir


def custom_step_fifosize(model, cfg):
    # TODO convert to NodeLocalTransformation
    # TODO handle chrc for input and output nodes
    all_act_tensors = [x.name for x in model.graph.value_info]
    for tensor_nm in all_act_tensors:
        # generate accumulated characteristic functions
        prod = model.find_producer(tensor_nm)
        cons = model.find_consumer(tensor_nm)
        if prod is None or cons is None:
            continue
        prod = getCustomOp(prod)
        period = prod.get_nodeattr("io_characteristic_period")
        prod_chrc = prod.get_nodeattr("io_characteristic")
        prod_chrc = np.asarray(prod_chrc).reshape(2, -1)[1]
        cons = getCustomOp(cons)
        cons_chrc = cons.get_nodeattr("io_characteristic")
        cons_chrc = np.asarray(cons_chrc).reshape(2, -1)[0]
        # find minimum phase shift satisfying the constraint
        pshift_min = period
        for pshift_cand in range(period):
            pshift_condition = [
                (prod_chrc[i + pshift_cand] >= cons_chrc[i])
                for i in range(period - pshift_cand)
            ]
            if all(pshift_condition):
                pshift_min = pshift_cand
                break
        fifo_depth = max(
            [(prod_chrc[i + pshift_cand] - cons_chrc[i]) for i in range(pshift_min)]
        )
        prod.set_nodeattr("outFIFODepth", fifo_depth)
        cons.set_nodeattr("inFIFODepth", fifo_depth)
    return model


def custom_step_fifocharacterize(model, cfg):
    model = model.transform(InsertDWC())
    model = model.transform(GiveUniqueNodeNames())
    model = model.transform(
        PrepareIP(cfg._resolve_fpga_part(), cfg._resolve_hls_clk_period())
    )
    model = model.transform(HLSSynthIP())
    model = model.transform(PrepareRTLSim())
    period = model.analysis(dataflow_performance)["max_cycles"] + 10
    model = model.transform(DeriveCharacteristic(period))
    return model


@pytest.mark.slow
@pytest.mark.vivado
def test_fifosizing():
    chkpt_name = pk.resource_filename("finn.qnn-data", "build_dataflow/model.onnx")
    tmp_output_dir = make_build_dir("build_fifosizing_")
    steps = build_cfg.default_build_dataflow_steps
    steps.insert(10, custom_step_fifocharacterize)
    steps.insert(11, custom_step_fifosize)
    cfg = build_cfg.DataflowBuildConfig(
        output_dir=tmp_output_dir,
        auto_fifo_depths=False,
        target_fps=10000,
        synth_clk_period_ns=10.0,
        board="Pynq-Z1",
        rtlsim_batch_size=100,
        shell_flow_type=build_cfg.ShellFlowType.VIVADO_ZYNQ,
        generate_outputs=[
            build_cfg.DataflowOutputType.ESTIMATE_REPORTS,
            build_cfg.DataflowOutputType.STITCHED_IP,
            build_cfg.DataflowOutputType.RTLSIM_PERFORMANCE,
        ],
        steps=steps,
        default_mem_mode=build_cfg.ComputeEngineMemMode.DECOUPLED,
    )
    build.build_dataflow_cfg(chkpt_name, cfg)
    with open(tmp_output_dir + "/report/estimate_network_performance.json") as f:
        est_data = json.load(f)
    with open(tmp_output_dir + "/report/rtlsim_performance.json") as f:
        sim_data = json.load(f)
    assert (
        float(sim_data["throughput[images/s]"])
        / float(est_data["estimated_throughput_fps"])
        > 0.9
    )
    shutil.rmtree(tmp_output_dir)