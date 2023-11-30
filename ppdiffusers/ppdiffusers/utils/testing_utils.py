# Copyright (c) 2023 PaddlePaddle Authors. All Rights Reserved.
# Copyright 2023 The HuggingFace Team. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import inspect
import io
import logging
import multiprocessing
import os
import random
import re
import struct
import tempfile
import unittest
import urllib.parse
from contextlib import contextmanager
from distutils.util import strtobool
from io import BytesIO, StringIO
from pathlib import Path
from typing import List, Optional, Union

import numpy as np
import PIL.Image
import PIL.ImageOps
import requests

from .import_utils import (
    BACKENDS_MAPPING,
    is_compel_available,
    is_fastdeploy_available,
    is_note_seq_available,
    is_opencv_available,
    is_paddle_available,
    is_paddle_version,
    is_paddlesde_available,
    is_torch_available,
)
from .logging import get_logger

global_rng = random.Random()

logger = get_logger(__name__)

if is_paddle_available():
    import paddle

    if "PPDIFFUSERS_TEST_DEVICE" in os.environ:
        paddle_device = os.environ["PPDIFFUSERS_TEST_DEVICE"]

        available_backends = ["gpu", "cpu"]
        if paddle_device not in available_backends:
            raise ValueError(
                f"unknown paddle backend for ppdiffusers tests: {paddle_device}. Available backends are:"
                f" {available_backends}"
            )
        logger.info(f"paddle_device overrode to {paddle_device}")
    else:
        paddle_device = "gpu" if paddle.is_compiled_with_cuda() else "cpu"


def image_grid(imgs, rows=None, cols=None):
    if rows is None and cols is None:
        rows = 1
        cols = len(imgs)
    assert len(imgs) == rows * cols
    w, h = imgs[0].size
    grid = PIL.Image.new("RGB", size=(cols * w, rows * h))

    for i, img in enumerate(imgs):
        grid.paste(img, box=(i % cols * w, i // cols * h))
    return grid


def paddle_all_close(a, b, *args, **kwargs):
    if not is_paddle_available():
        raise ValueError("Paddle needs to be installed to use this function.")
    if not paddle.allclose(a, b, *args, **kwargs):
        assert False, f"Max diff is absolute {(a - b).abs().max()}. Diff tensor is {(a - b).abs()}."
    return True


def print_tensor_test(tensor, filename="test_corrections.txt", expected_tensor_name="expected_slice"):
    test_name = os.environ.get("PYTEST_CURRENT_TEST")
    if not paddle.is_tensor(tensor):
        tensor = paddle.to_tensor(tensor)

    tensor_str = str(tensor.detach().cpu().flatten().cast("float32")).replace("\n", "")
    output_str = tensor_str.replace("tensor", f"{expected_tensor_name} = np.array")
    test_file, test_class, test_fn = test_name.split("::")
    test_fn = test_fn.split()[0]
    with open(filename, "a") as f:
        print(";".join([test_file, test_class, test_fn, output_str]), file=f)


def get_tests_dir(append_path=None):
    """
    Args:
        append_path: optional path to append to the tests dir path
    Return:
        The full path to the `tests` dir, so that the tests can be invoked from anywhere. Optionally `append_path` is
        joined after the `tests` dir the former is provided.
    """
    # this function caller's __file__
    caller__file__ = inspect.stack()[1][1]
    tests_dir = os.path.abspath(os.path.dirname(caller__file__))

    while not tests_dir.endswith("tests"):
        tests_dir = os.path.dirname(tests_dir)

    if append_path:
        return os.path.join(tests_dir, append_path)
    else:
        return tests_dir


def parse_flag_from_env(key, default=False):
    try:
        value = os.environ[key]
    except KeyError:
        # KEY isn't set, default to `default`.
        _value = default
    else:
        # KEY is set, convert it to True or False.
        try:
            _value = strtobool(value)
        except ValueError:
            # More values are supported, but let's keep the message simple.
            raise ValueError(f"If set, {key} must be yes or no.")
    return _value


_run_slow_tests = parse_flag_from_env("RUN_SLOW", default=False)
_run_nightly_tests = parse_flag_from_env("RUN_NIGHTLY", default=False)


def floats_tensor(shape, scale=1.0, rng=None, name=None):
    """Creates a random float32 tensor"""
    if rng is None:
        rng = global_rng

    total_dims = 1
    for dim in shape:
        total_dims *= dim

    values = []
    for _ in range(total_dims):
        values.append(rng.random() * scale)

    return paddle.to_tensor(values, dtype=paddle.float32).reshape(shape)


def slow(test_case):
    """
    Decorator marking a test as slow.

    Slow tests are skipped by default. Set the RUN_SLOW environment variable to a truthy value to run them.

    """
    return unittest.skipUnless(_run_slow_tests, "test is slow")(test_case)


def nightly(test_case):
    """
    Decorator marking a test that runs nightly in the ppdiffusers CI.

    Slow tests are skipped by default. Set the RUN_NIGHTLY environment variable to a truthy value to run them.

    """
    return unittest.skipUnless(_run_nightly_tests, "test is nightly")(test_case)


def require_paddle_2_5(test_case):
    """
    Decorator marking a test that requires Paddle 2.5. These tests are skipped when it isn't installed.
    """
    return unittest.skipUnless(is_paddle_available() and is_paddle_version(">=", "2.5.0"), "test requires Paddle 2.5")(
        test_case
    )


def require_paddle(test_case):
    """
    Decorator marking a test that requires Paddle. These tests are skipped when Paddle isn't installed.
    """
    return unittest.skipUnless(is_paddle_available(), "test requires Paddle")(test_case)


def require_torch(test_case):
    """Decorator marking a test that requires TORCH."""
    return unittest.skipUnless(is_torch_available(), "test requires TORCH")(test_case)


def require_paddle_gpu(test_case):
    """Decorator marking a test that requires CUDA and Paddle."""
    return unittest.skipUnless(is_paddle_available() and paddle_device == "gpu", "test requires Paddle+CUDA")(
        test_case
    )


def require_compel(test_case):
    """
    Decorator marking a test that requires compel: https://github.com/damian0815/compel. These tests are skipped when
    the library is not installed.
    """
    return unittest.skipUnless(is_compel_available(), "test requires compel")(test_case)


def require_fastdeploy(test_case):
    """
    Decorator marking a test that requires fastdeploy. These tests are skipped when fastdeploy isn't installed.
    """
    return unittest.skipUnless(is_fastdeploy_available(), "test requires fastdeploy")(test_case)


def require_note_seq(test_case):
    """
    Decorator marking a test that requires note_seq. These tests are skipped when note_seq isn't installed.
    """
    return unittest.skipUnless(is_note_seq_available(), "test requires note_seq")(test_case)


def require_paddlesde(test_case):
    """
    Decorator marking a test that requires paddlesde. These tests are skipped when paddlesde isn't installed.
    """
    return unittest.skipUnless(is_paddlesde_available(), "test requires paddlesde")(test_case)


def load_numpy(arry: Union[str, np.ndarray], local_path: Optional[str] = None) -> np.ndarray:
    if isinstance(arry, str):
        # local_path = "/home/patrick_huggingface_co/"
        if local_path is not None:
            # local_path can be passed to correct images of tests
            return os.path.join(local_path, "/".join([arry.split("/")[-5], arry.split("/")[-2], arry.split("/")[-1]]))
        elif arry.startswith("http://") or arry.startswith("https://"):
            response = requests.get(arry)
            response.raise_for_status()
            arry = np.load(BytesIO(response.content))
        elif os.path.isfile(arry):
            arry = np.load(arry)
        else:
            raise ValueError(
                f"Incorrect path or url, URLs must start with `http://` or `https://`, and {arry} is not a valid path"
            )
    elif isinstance(arry, np.ndarray):
        pass
    else:
        raise ValueError(
            "Incorrect format used for numpy ndarray. Should be an url linking to an image, a local path, or a"
            " ndarray."
        )

    return arry


def load_pt(url: str):
    if is_torch_available():
        import torch

        response = requests.get(url)
        response.raise_for_status()
        arry = torch.load(BytesIO(response.content), map_location="cpu")
        return arry
    else:
        raise ValueError("Please install torch firstly!")


def load_pd(url: str):
    response = requests.get(url)
    response.raise_for_status()
    arry = paddle.load(BytesIO(response.content))
    return arry


def load_image(image: Union[str, PIL.Image.Image]) -> PIL.Image.Image:
    """
    Loads `image` to a PIL Image.
    Args:
        image (`str` or `PIL.Image.Image`):
            The image to convert to the PIL Image format.
    Returns:
        `PIL.Image.Image`:
            A PIL Image.
    """
    if isinstance(image, str):
        if image.startswith("http://") or image.startswith("https://"):
            image = PIL.Image.open(requests.get(image, stream=True).raw)
        elif os.path.isfile(image):
            image = PIL.Image.open(image)
        else:
            raise ValueError(
                f"Incorrect path or url, URLs must start with `http://` or `https://`, and {image} is not a valid path"
            )
    elif isinstance(image, PIL.Image.Image):
        image = image
    else:
        raise ValueError(
            "Incorrect format used for image. Should be an url linking to an image, a local path, or a PIL image."
        )
    image = PIL.ImageOps.exif_transpose(image)
    image = image.convert("RGB")
    return image


def preprocess_image(image: PIL.Image, batch_size: int):
    w, h = image.size
    w, h = (x - x % 8 for x in (w, h))  # resize to integer multiple of 8
    image = image.resize((w, h), resample=PIL.Image.LANCZOS)
    image = np.array(image).astype(np.float32) / 255.0
    image = np.vstack([image[None].transpose(0, 3, 1, 2)] * batch_size)
    image = paddle.to_tensor(image)
    return 2.0 * image - 1.0


def export_to_gif(image: List[PIL.Image.Image], output_gif_path: str = None) -> str:
    if output_gif_path is None:
        output_gif_path = tempfile.NamedTemporaryFile(suffix=".gif").name

    image[0].save(
        output_gif_path,
        save_all=True,
        append_images=image[1:],
        optimize=False,
        duration=100,
        loop=0,
    )
    return output_gif_path


@contextmanager
def buffered_writer(raw_f):
    f = io.BufferedWriter(raw_f)
    yield f
    f.flush()


def export_to_ply(mesh, output_ply_path: str = None):
    """
    Write a PLY file for a mesh.
    """
    if output_ply_path is None:
        output_ply_path = tempfile.NamedTemporaryFile(suffix=".ply").name

    coords = mesh.verts.detach().cpu().numpy()
    faces = mesh.faces.cpu().numpy()
    rgb = np.stack([mesh.vertex_channels[x].detach().cpu().numpy() for x in "RGB"], axis=1)

    with buffered_writer(open(output_ply_path, "wb")) as f:
        f.write(b"ply\n")
        f.write(b"format binary_little_endian 1.0\n")
        f.write(bytes(f"element vertex {len(coords)}\n", "ascii"))
        f.write(b"property float x\n")
        f.write(b"property float y\n")
        f.write(b"property float z\n")
        if rgb is not None:
            f.write(b"property uchar red\n")
            f.write(b"property uchar green\n")
            f.write(b"property uchar blue\n")
        if faces is not None:
            f.write(bytes(f"element face {len(faces)}\n", "ascii"))
            f.write(b"property list uchar int vertex_index\n")
        f.write(b"end_header\n")

        if rgb is not None:
            rgb = (rgb * 255.499).round().astype(int)
            vertices = [
                (*coord, *rgb)
                for coord, rgb in zip(
                    coords.tolist(),
                    rgb.tolist(),
                )
            ]
            format = struct.Struct("<3f3B")
            for item in vertices:
                f.write(format.pack(*item))
        else:
            format = struct.Struct("<3f")
            for vertex in coords.tolist():
                f.write(format.pack(*vertex))

        if faces is not None:
            format = struct.Struct("<B3I")
            for tri in faces.tolist():
                f.write(format.pack(len(tri), *tri))

    return output_ply_path


def export_to_obj(mesh, output_obj_path: str = None):
    if output_obj_path is None:
        output_obj_path = tempfile.NamedTemporaryFile(suffix=".obj").name

    verts = mesh.verts.detach().cpu().numpy()
    faces = mesh.faces.cpu().numpy()

    vertex_colors = np.stack([mesh.vertex_channels[x].detach().cpu().numpy() for x in "RGB"], axis=1)
    vertices = [
        "{} {} {} {} {} {}".format(*coord, *color) for coord, color in zip(verts.tolist(), vertex_colors.tolist())
    ]

    faces = ["f {} {} {}".format(str(tri[0] + 1), str(tri[1] + 1), str(tri[2] + 1)) for tri in faces.tolist()]

    combined_data = ["v " + vertex for vertex in vertices] + faces

    with open(output_obj_path, "w") as f:
        f.writelines("\n".join(combined_data))


def export_to_video(video_frames: List[np.ndarray], output_video_path: str = None) -> str:
    if is_opencv_available():
        import cv2
    else:
        raise ImportError(BACKENDS_MAPPING["opencv"][1].format("export_to_video"))
    if output_video_path is None:
        output_video_path = tempfile.NamedTemporaryFile(suffix=".mp4").name

    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    h, w, c = video_frames[0].shape
    video_writer = cv2.VideoWriter(output_video_path, fourcc, fps=8, frameSize=(w, h))
    for i in range(len(video_frames)):
        img = cv2.cvtColor(video_frames[i], cv2.COLOR_RGB2BGR)
        video_writer.write(img)
    return output_video_path


def load_hf_numpy(path) -> np.ndarray:
    if not path.startswith("http://") or path.startswith("https://"):
        HF_ENDPOINT = os.getenv("HF_ENDPOINT", "https://huggingface.co").rstrip("/")
        path = os.path.join(
            f"{HF_ENDPOINT}/datasets/fusing/diffusers-testing/resolve/main",
            urllib.parse.quote(path),
        )

    return load_numpy(path)


def load_ppnlp_numpy(path) -> np.ndarray:
    if not path.startswith("http://") or path.startswith("https://"):
        path = os.path.join(
            "https://paddlenlp.bj.bcebos.com/models/community/CompVis/data/diffusers-testing", urllib.parse.quote(path)
        )
    return load_numpy(path)


# --- pytest conf functions --- #

# to avoid multiple invocation from tests/conftest.py and examples/conftest.py - make sure it's called only once
pytest_opt_registered = {}


def pytest_addoption_shared(parser):
    """
    This function is to be called from `conftest.py` via `pytest_addoption` wrapper that has to be defined there.

    It allows loading both `conftest.py` files at once without causing a failure due to adding the same `pytest`
    option.

    """
    option = "--make-reports"
    if option not in pytest_opt_registered:
        parser.addoption(
            option,
            action="store",
            default=False,
            help="generate report files. The value of this option is used as a prefix to report names",
        )
        pytest_opt_registered[option] = 1


def pytest_terminal_summary_main(tr, id):
    """
    Generate multiple reports at the end of test suite run - each report goes into a dedicated file in the current
    directory. The report files are prefixed with the test suite name.

    This function emulates --duration and -rA pytest arguments.

    This function is to be called from `conftest.py` via `pytest_terminal_summary` wrapper that has to be defined
    there.

    Args:
    - tr: `terminalreporter` passed from `conftest.py`
    - id: unique id like `tests` or `examples` that will be incorporated into the final reports filenames - this is
      needed as some jobs have multiple runs of pytest, so we can't have them overwrite each other.

    NB: this functions taps into a private _pytest API and while unlikely, it could break should
    pytest do internal changes - also it calls default internal methods of terminalreporter which
    can be hijacked by various `pytest-` plugins and interfere.

    """
    from _pytest.config import create_terminal_writer

    if not len(id):
        id = "tests"

    config = tr.config
    orig_writer = config.get_terminal_writer()
    orig_tbstyle = config.option.tbstyle
    orig_reportchars = tr.reportchars

    dir = "reports"
    Path(dir).mkdir(parents=True, exist_ok=True)
    report_files = {
        k: f"{dir}/{id}_{k}.txt"
        for k in [
            "durations",
            "errors",
            "failures_long",
            "failures_short",
            "failures_line",
            "passes",
            "stats",
            "summary_short",
            "warnings",
        ]
    }

    # custom durations report
    # note: there is no need to call pytest --durations=XX to get this separate report
    # adapted from https://github.com/pytest-dev/pytest/blob/897f151e/src/_pytest/runner.py#L66
    dlist = []
    for replist in tr.stats.values():
        for rep in replist:
            if hasattr(rep, "duration"):
                dlist.append(rep)
    if dlist:
        dlist.sort(key=lambda x: x.duration, reverse=True)
        with open(report_files["durations"], "w") as f:
            durations_min = 0.05  # sec
            f.write("slowest durations\n")
            for i, rep in enumerate(dlist):
                if rep.duration < durations_min:
                    f.write(f"{len(dlist)-i} durations < {durations_min} secs were omitted")
                    break
                f.write(f"{rep.duration:02.2f}s {rep.when:<8} {rep.nodeid}\n")

    def summary_failures_short(tr):
        # expecting that the reports were --tb=long (default) so we chop them off here to the last frame
        reports = tr.getreports("failed")
        if not reports:
            return
        tr.write_sep("=", "FAILURES SHORT STACK")
        for rep in reports:
            msg = tr._getfailureheadline(rep)
            tr.write_sep("_", msg, red=True, bold=True)
            # chop off the optional leading extra frames, leaving only the last one
            longrepr = re.sub(r".*_ _ _ (_ ){10,}_ _ ", "", rep.longreprtext, 0, re.M | re.S)
            tr._tw.line(longrepr)
            # note: not printing out any rep.sections to keep the report short

        # use ready-made report funcs, we are just hijacking the filehandle to log to a dedicated file each
        # adapted from https://github.com/pytest-dev/pytest/blob/897f151e/src/_pytest/terminal.py#L814
        # note: some pytest plugins may interfere by hijacking the default `terminalreporter` (e.g.
        # pytest-instafail does that)

        # report failures with line/short/long styles

    config.option.tbstyle = "auto"  # full tb
    with open(report_files["failures_long"], "w") as f:
        tr._tw = create_terminal_writer(config, f)
        tr.summary_failures()

    # config.option.tbstyle = "short" # short tb
    with open(report_files["failures_short"], "w") as f:
        tr._tw = create_terminal_writer(config, f)
        summary_failures_short(tr)

    config.option.tbstyle = "line"  # one line per error
    with open(report_files["failures_line"], "w") as f:
        tr._tw = create_terminal_writer(config, f)
        tr.summary_failures()

    with open(report_files["errors"], "w") as f:
        tr._tw = create_terminal_writer(config, f)
        tr.summary_errors()

    with open(report_files["warnings"], "w") as f:
        tr._tw = create_terminal_writer(config, f)
        tr.summary_warnings()  # normal warnings
        tr.summary_warnings()  # final warnings

    tr.reportchars = "wPpsxXEf"  # emulate -rA (used in summary_passes() and short_test_summary())
    with open(report_files["passes"], "w") as f:
        tr._tw = create_terminal_writer(config, f)
        tr.summary_passes()

    with open(report_files["summary_short"], "w") as f:
        tr._tw = create_terminal_writer(config, f)
        tr.short_test_summary()

    with open(report_files["stats"], "w") as f:
        tr._tw = create_terminal_writer(config, f)
        tr.summary_stats()

    # restore:
    tr._tw = orig_writer
    tr.reportchars = orig_reportchars
    config.option.tbstyle = orig_tbstyle


# Taken from: https://github.com/huggingface/transformers/blob/3658488ff77ff8d45101293e749263acf437f4d5/src/transformers/testing_utils.py#L1787
def run_test_in_subprocess(test_case, target_func, inputs=None, timeout=None):
    """
    To run a test in a subprocess. In particular, this can avoid (GPU) memory issue.
    Args:
        test_case (`unittest.TestCase`):
            The test that will run `target_func`.
        target_func (`Callable`):
            The function implementing the actual testing logic.
        inputs (`dict`, *optional*, defaults to `None`):
            The inputs that will be passed to `target_func` through an (input) queue.
        timeout (`int`, *optional*, defaults to `None`):
            The timeout (in seconds) that will be passed to the input and output queues. If not specified, the env.
            variable `PYTEST_TIMEOUT` will be checked. If still `None`, its value will be set to `600`.
    """
    if timeout is None:
        timeout = int(os.environ.get("PYTEST_TIMEOUT", 600))

    start_methohd = "spawn"
    ctx = multiprocessing.get_context(start_methohd)

    input_queue = ctx.Queue(1)
    output_queue = ctx.JoinableQueue(1)

    # We can't send `unittest.TestCase` to the child, otherwise we get issues regarding pickle.
    input_queue.put(inputs, timeout=timeout)

    process = ctx.Process(target=target_func, args=(input_queue, output_queue, timeout))
    process.start()
    # Kill the child process if we can't get outputs from it in time: otherwise, the hanging subprocess prevents
    # the test to exit properly.
    try:
        results = output_queue.get(timeout=timeout)
        output_queue.task_done()
    except Exception as e:
        process.terminate()
        test_case.fail(e)
    process.join(timeout=timeout)

    if results["error"] is not None:
        test_case.fail(f'{results["error"]}')


class CaptureLogger:
    """
    Args:
    Context manager to capture `logging` streams
        logger: 'logging` logger object
    Returns:
        The captured output is available via `self.out`
    Example:
    ```python
    >>> from ppdiffusers import logging
    >>> from ppdiffusers.testing_utils import CaptureLogger

    >>> msg = "Testing 1, 2, 3"
    >>> logging.set_verbosity_info()
    >>> logger = logging.get_logger("ppdiffusers.pipelines.stable_diffusion.pipeline_stable_diffusion.py")
    >>> with CaptureLogger(logger) as cl:
    ...     logger.info(msg)
    >>> assert cl.out, msg + "\n"
    ```
    """

    def __init__(self, logger):
        self.logger = logger
        self.io = StringIO()
        self.sh = logging.StreamHandler(self.io)
        self.out = ""

    def __enter__(self):
        self.logger.addHandler(self.sh)
        return self

    def __exit__(self, *exc):
        self.logger.removeHandler(self.sh)
        self.out = self.io.getvalue()

    def __repr__(self):
        return f"captured: {self.out}\n"


def enable_full_determinism():
    """
    Helper function for reproducible behavior during distributed training. See
    - https://pytorch.org/docs/stable/notes/randomness.html
    - https://www.paddlepaddle.org.cn/documentation/docs/zh/develop/dev_guides/api_contributing_guides/new_cpp_op_cn.html#suanzishuzhiwendingxingwenti
    """
    #  Enable deterministic mode. This potentially requires either the environment
    #  variable 'CUDA_LAUNCH_BLOCKING' or 'CUBLAS_WORKSPACE_CONFIG' to be set,
    # depending on the CUDA version, so we set them both here

    os.environ["CUDA_LAUNCH_BLOCKING"] = "1"
    os.environ["CUBLAS_WORKSPACE_CONFIG"] = ":16:8"

    # Enable CUDNN deterministic mode
    os.environ["FLAGS_cudnn_deterministic"] = "True"
    os.environ["FLAGS_cpu_deterministic"] = "True"
    os.environ["NVIDIA_TF32_OVERRIDE"] = "0"


def disable_full_determinism():
    os.environ["CUDA_LAUNCH_BLOCKING"] = "0"
    os.environ["CUBLAS_WORKSPACE_CONFIG"] = ""

    # Disable CUDNN deterministic mode
    os.environ["FLAGS_cudnn_deterministic"] = "False"
    os.environ["FLAGS_cpu_deterministic"] = "False"
    os.environ["NVIDIA_TF32_OVERRIDE"] = "1"
