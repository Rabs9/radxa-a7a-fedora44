#!/bin/bash
# convert-kws-encoder.sh: inside Allwinner's ACUITY container (ubuntu-npu:v2.0.10.2), the frozen English KWS encoder
# (output/npu-kws/onnx_fixed/encoder.onnx, made by output/npu-kws/tools/freeze.py: N=1, warm, batch-first caches)
# -> a float (fp16) NBG for the A733's VIP9000NanoDI Plus, plus ACUITY's float32 simulation of dataset.txt (the
# check inputs) in sim_out/ to compare with onnxruntime. Run:
#   podman run --rm --network none -v "$PWD"/output/npu-kws/acuity:/workspace:z \
#     -v "$PWD"/tools/npu:/tools:ro,z ubuntu-npu:v2.0.10.2 bash /tools/convert-kws-encoder.sh
set -euo pipefail
export ACUITY_PATH=/usr/local/acuity_command_line_tools
export VIV_SDK=/root/Vivante_IDE/VivanteIDE5.11.0/cmdtools
# Radxa's setting for their zipformer KWS (Gather is kept as Gather)
export VIV_VX_ENABLE_GRAPH_TRANSFORM="-Dump[transform.rewrite.gather_to_stride_slice=0]"
TARGET=VIP9000NANODI_PLUS_PID0X1000003B
P="python3 $ACUITY_PATH/pegasus.py"
cd /workspace/encoder
rm -rf encoder.json encoder.data wksp sim_out encoder_inputmeta.yml
$P import onnx --model encoder.onnx --output-model encoder.json --output-data encoder.data
$P generate inputmeta --model encoder.json --input-meta-output encoder_inputmeta.yml
# tensors, not images: no channel reversal, no preprocessing
sed -i 's/reverse_channel: true/reverse_channel: false/; s/category: image/category: undefined/' encoder_inputmeta.yml
if [ -f dataset.txt ]; then
  $P inference --model encoder.json --model-data encoder.data --dtype float32 --with-input-meta encoder_inputmeta.yml \
    --output-dir sim_out --iterations 1 > sim.log 2>&1 || { tail -5 sim.log; echo "simulation failed (export goes on)"; }
fi
$P export ovxlib --model encoder.json --model-data encoder.data --dtype float --with-input-meta encoder_inputmeta.yml \
  --target-ide-project linux64 --pack-nbg-unify --optimize "$TARGET" --viv-sdk "$VIV_SDK" \
  --output-path wksp/encoder_fp16/encoder
find wksp -name '*.nb' -exec ls -l {} +
