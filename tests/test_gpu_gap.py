"""Tests for tools/board/a7a-gpu-gap, the GPU gap report (docs/GPU-GAP-2026-09-10.md): capability snapshots of the
open stack (Mesa powervr Vulkan + zink) and the vendor DDK, the comparison of the two, and the Mesa version gates
(src/mesa/main/version.c) evaluated against what each stack exposes. Everything here runs on the laptop: the
parsers get small hand-written samples in the exact shape the board's tools print, the collectors get a fake
command runner and a fake file reader."""

import importlib.machinery
import importlib.util
import json
import os
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

P = Path(__file__).resolve().parent.parent
SCRIPT = P / "tools" / "board" / "a7a-gpu-gap"
GATES = P / "tools" / "board" / "a7a-gpu-gap-mesa-26.1.8-gates.json"

_loader = importlib.machinery.SourceFileLoader("a7a_gpu_gap", str(SCRIPT))
gap = importlib.util.module_from_spec(importlib.util.spec_from_file_location("a7a_gpu_gap", str(SCRIPT), loader=_loader))
_loader.exec_module(gap)

EGLINFO = textwrap.dedent("""\
    EGL client extensions string:
        EGL_EXT_client_extensions, EGL_EXT_device_base, EGL_MESA_platform_surfaceless

    GBM platform:
    EGL API version: 1.5
    EGL vendor string: Mesa Project
    EGL version string: 1.5
    EGL client APIs: OpenGL OpenGL_ES
    EGL driver name: powervr
    EGL extensions string:
        EGL_ANDROID_blob_cache, EGL_EXT_buffer_age,
        EGL_KHR_no_config_context
    OpenGL compatibility profile vendor: Mesa
    OpenGL compatibility profile renderer: zink Vulkan 1.2(PowerVR B-Series BXM-4-64 MC1 (IMAGINATION_OPEN_SOURCE_MESA))
    OpenGL compatibility profile version: 2.1 Mesa 26.1.8
    OpenGL compatibility profile shading language version: 1.20
    OpenGL compatibility profile extensions:
        GL_ARB_shadow, GL_ARB_vertex_shader,
        GL_EXT_texture_sRGB
    OpenGL ES profile vendor: Mesa
    OpenGL ES profile renderer: zink Vulkan 1.2(PowerVR B-Series BXM-4-64 MC1 (IMAGINATION_OPEN_SOURCE_MESA))
    OpenGL ES profile version: OpenGL ES 2.0 Mesa 26.1.8
    OpenGL ES profile shading language version: OpenGL ES GLSL ES 1.0.16
    OpenGL ES profile extensions:
        GL_OES_texture_float, GL_EXT_sRGB

    Configurations:
         bf lv colorbuffer dp st  ms    vis   cav bi  renderable  supported
      id sz  l  r  g  b  a th cl ns b    id   eat nd gl es es2 vg surfaces
    ---------------------------------------------------------------------
    0x01 32  0  8  8  8  8  0  0  0 0 0x34325241--      y  y  y     win,pb

    X11 platform:
    eglinfo: eglInitialize failed

    Surfaceless platform:
    EGL API version: 1.5
    EGL vendor string: Mesa Project
    EGL version string: 1.5
    EGL client APIs: OpenGL OpenGL_ES
    EGL driver name: powervr
    EGL extensions string:
        EGL_KHR_no_config_context
    OpenGL compatibility profile vendor: Mesa
    OpenGL compatibility profile renderer: zink
    OpenGL compatibility profile version: 2.1 Mesa 26.1.8
    OpenGL compatibility profile shading language version: 1.20
    OpenGL compatibility profile extensions:
        GL_ARB_shadow
    OpenGL ES profile vendor: Mesa
    OpenGL ES profile renderer: zink
    OpenGL ES profile version: OpenGL ES 2.0 Mesa 26.1.8
    OpenGL ES profile shading language version: OpenGL ES GLSL ES 1.0.16
    OpenGL ES profile extensions:
        GL_OES_texture_float

    Device platform:
    Device #0:

    EGL device extensions string:
        EGL_EXT_device_drm, EGL_EXT_device_drm_render_node
    Platform Device platform:
    EGL API version: 1.5
    EGL vendor string: Mesa Project
    EGL version string: 1.5
    EGL client APIs: OpenGL OpenGL_ES
    EGL driver name: powervr
    EGL extensions string:
        EGL_KHR_no_config_context
    OpenGL core profile vendor: Mesa
    OpenGL core profile renderer: llvmpipe
    OpenGL core profile version: 4.5 (Core Profile) Mesa 26.1.8
    OpenGL core profile shading language version: 4.50
    OpenGL core profile extensions:
        GL_ARB_gpu_shader5
    Device #1:

    EGL device extensions string:
        EGL_EXT_device_drm_render_node, EGL_MESA_device_software
    Platform Device platform:
    EGL API version: 1.5
    EGL vendor string: Mesa Project
    EGL version string: 1.5
    EGL client APIs: OpenGL OpenGL_ES
    EGL driver name: swrast
    EGL extensions string:
        EGL_KHR_no_config_context
    OpenGL ES profile vendor: Mesa
    OpenGL ES profile renderer: llvmpipe (LLVM 21.1.8, 128 bits)
    OpenGL ES profile version: OpenGL ES 3.2 Mesa 26.1.8
    OpenGL ES profile shading language version: OpenGL ES GLSL ES 3.20
    OpenGL ES profile extensions:
        GL_EXT_transform_feedback_dummy
    """)

GLXINFO = textwrap.dedent("""\
    name of display: :0
    display: :0  screen: 0
    direct rendering: Yes
    Extended renderer info (GLX_MESA_query_renderer):
        Vendor: Mesa (0x1010)
        Device: zink Vulkan 1.2(PowerVR B-Series BXM-4-64 MC1 (IMAGINATION_OPEN_SOURCE_MESA)) (0x36104183)
        Version: 26.1.8
        Accelerated: yes
        Preferred profile: compat (0x2)
        Max core profile version: 0.0
        Max compat profile version: 2.1
        Max GLES1 profile version: 1.1
        Max GLES[23] profile version: 2.0
    OpenGL vendor string: Mesa
    OpenGL renderer string: zink Vulkan 1.2(PowerVR B-Series BXM-4-64 MC1 (IMAGINATION_OPEN_SOURCE_MESA))
    OpenGL version string: 2.1 Mesa 26.1.8
    OpenGL shading language version string: 1.20
    OpenGL extensions:
        GL_ARB_shadow, GL_ARB_vertex_shader,
        GL_EXT_texture_sRGB
    OpenGL limits:
        GL_MAX_TEXTURE_SIZE = 4096
        GL_MAX_3D_TEXTURE_SIZE = 256
      GL_EXT_texture_array:
        GL_MAX_ARRAY_TEXTURE_LAYERS = 256
        GL_MAX_VIEWPORT_DIMS = 4096, 4096
      GL_ARB_framebuffer_object:
        GL_MAX_COLOR_ATTACHMENTS = 4
        GL_MAX_SAMPLES = 4

    OpenGL ES profile version string: OpenGL ES 2.0 Mesa 26.1.8
    OpenGL ES profile shading language version string: OpenGL ES GLSL ES 1.0.16
    OpenGL ES profile extensions:
        GL_OES_texture_float, GL_EXT_sRGB
    OpenGL ES profile limits:
        GL_MAX_TEXTURE_SIZE = 4096

    120 GLX Visuals
    """)

# the shape of `vulkaninfo --json` (a Vulkan profiles file), cut down to what the tool reads
VP = {
    "capabilities": {"device": {
        "extensions": {"VK_KHR_swapchain": 70, "VK_EXT_custom_border_color": 12},
        "features": {
            "VkPhysicalDeviceFeatures": {"geometryShader": False, "robustBufferAccess": True, "depthClamp": False},
            "VkPhysicalDeviceVulkan12Features": {"timelineSemaphore": True},
        },
        "properties": {
            "VkPhysicalDeviceProperties": {
                "apiVersion": 4202850, "deviceName": "PowerVR B-Series BXM-4-64 MC1", "driverVersion": 109056008,
                "deviceType": "VK_PHYSICAL_DEVICE_TYPE_INTEGRATED_GPU", "vendorID": 4112, "deviceID": 907035011,
                "limits": {"maxUniformBufferRange": 16384, "maxPerStageDescriptorUniformBuffers": 12,
                           "maxImageDimension2D": 4096, "minMemoryMapAlignment": 4096,
                           "framebufferColorSampleCounts": ["VK_SAMPLE_COUNT_1_BIT", "VK_SAMPLE_COUNT_4_BIT"],
                           "pointSizeRange": [1.0, 511.0], "timestampComputeAndGraphics": False},
            },
            "VkPhysicalDeviceDriverProperties": {"driverName": "Imagination open-source Mesa driver",
                                                  "driverInfo": "Mesa 26.1.8"},
        },
        "formats": {
            "VK_FORMAT_R8G8B8A8_UNORM": {"VkFormatProperties": {
                "linearTilingFeatures": ["VK_FORMAT_FEATURE_SAMPLED_IMAGE_BIT"],
                "optimalTilingFeatures": ["VK_FORMAT_FEATURE_SAMPLED_IMAGE_BIT", "VK_FORMAT_FEATURE_COLOR_ATTACHMENT_BIT"],
                "bufferFeatures": []}},
        },
    }},
    "profiles": {"VP_VULKANINFO_x": {"api-version": "1.2.354", "label": "PowerVR B-Series BXM-4-64 MC1 driver 26.1.8"}},
}

VERSION_C = textwrap.dedent("""\
    static GLuint
    compute_version(const struct gl_extensions *extensions,
                    const struct gl_constants *consts, gl_api api)
    {
       GLuint major, minor, version;

       const bool ver_1_4 = (extensions->ARB_shadow);
       const bool ver_1_5 = ver_1_4;
       const bool ver_2_0 = (ver_1_5 &&
                             extensions->ARB_vertex_shader);
       /* a comment that must not confuse the parser && */
       const bool ver_3_0 = (ver_2_0 &&
                             consts->GLSLVersion >= 130 &&
                             (consts->MaxSamples >= 4 || consts->FakeSWMSAA) &&
                             (api == API_OPENGL_CORE ||
                              extensions->ARB_color_buffer_float) &&
                             extensions->EXT_transform_feedback);
       const bool ver_3_1 = (ver_3_0 &&
                             consts->Program[MESA_SHADER_VERTEX].MaxTextureImageUnits >= 16);

       if (ver_3_1) {
          major = 3;
       }
       return version;
    }

    static GLuint
    compute_version_es2(const struct gl_extensions *extensions,
                        const struct gl_constants *consts)
    {
       const bool ver_2_0 = (extensions->ARB_vertex_shader);
       const bool ver_3_0 = (extensions->EXT_transform_feedback &&
                             extensions->ARB_uniform_buffer_object &&
                             consts->MaxColorAttachments >= 4);
       const bool es31_compute_shader =
          consts->MaxComputeWorkGroupInvocations >= 128 &&
          consts->Program[MESA_SHADER_COMPUTE].MaxShaderStorageBlocks;
       const bool ver_3_1 = (ver_3_0 &&
                             es31_compute_shader);

       if (ver_3_1) {
          return 31;
       }
       return 0;
    }
    """)

EXT_TABLE = textwrap.dedent("""\
    #define GLL 0
    #define GLC 0
    #define ES1 0
    #define ES2 0
    #define  x ~0
    EXT(ARB_shadow                              , ARB_shadow                             , GLL,  x ,  x ,  x , 2001)
    EXT(ARB_uniform_buffer_object               , ARB_uniform_buffer_object              , GLL, GLC,  x ,  x , 2009)
    EXT(EXT_transform_feedback                  , EXT_transform_feedback                 , GLL, GLC,  x ,  x , 2011)
    EXT(ARB_gpu_shader5                         , ARB_gpu_shader5                        ,  32, GLC,  x ,  x , 2010)
    EXT(OES_geometry_shader                     , OES_geometry_shader                    ,  x ,  x ,  x ,  31, 2015)
    EXT(EXT_geometry_shader                     , OES_geometry_shader                    ,  x ,  x ,  x ,  31, 2015)
    EXT(OES_texture_float                       , OES_texture_float                      ,  x ,  x ,  x , ES2, 2005)
    """)


def vk_view(**over):
    """a normalized Vulkan view, VP above with overrides: features/limits/extensions dicts are merged"""
    v = gap.normalize_vulkan(json.loads(json.dumps(VP)))
    for k, val in over.items():
        if isinstance(val, dict) and isinstance(v.get(k), dict):
            v[k].update(val)
        else:
            v[k] = val
    return v


class Parsers(unittest.TestCase):
    def test_eglinfo_platforms_profiles_and_extensions(self):
        e = gap.parse_eglinfo(EGLINFO)
        names = [p["name"] for p in e["platforms"]]
        self.assertEqual(names, ["GBM", "X11", "Surfaceless", "Device #0", "Device #1"])
        gbm = e["platforms"][0]
        self.assertEqual(gbm["egl"]["driver"], "powervr")
        self.assertEqual(gbm["egl"]["version"], "1.5")
        self.assertIn("EGL_KHR_no_config_context", gbm["egl"]["extensions"])
        self.assertEqual(gbm["gl"]["compat"]["version"], "2.1 Mesa 26.1.8")
        self.assertEqual(gbm["gl"]["compat"]["extensions"], ["GL_ARB_shadow", "GL_ARB_vertex_shader", "GL_EXT_texture_sRGB"])
        self.assertEqual(gbm["gl"]["es"]["version"], "OpenGL ES 2.0 Mesa 26.1.8")
        self.assertEqual(gbm["gl"]["es"]["extensions"], ["GL_OES_texture_float", "GL_EXT_sRGB"])
        self.assertIsNone(gbm["gl"].get("core"))
        # the configuration table is not an extension list
        self.assertFalse(any("0x01" in x for x in gbm["gl"]["es"]["extensions"]))

    def test_eglinfo_failed_platform_and_devices(self):
        e = gap.parse_eglinfo(EGLINFO)
        x11 = e["platforms"][1]
        self.assertIn("eglInitialize failed", x11["error"])
        self.assertEqual(x11["gl"], {})
        d0, d1 = e["platforms"][3], e["platforms"][4]
        self.assertEqual(d0["gl"]["core"]["version"], "4.5 (Core Profile) Mesa 26.1.8")
        self.assertEqual(d1["egl"]["driver"], "swrast")
        self.assertIn("EGL_MESA_device_software", d1["device_extensions"])

    def test_probe_context_api_is_the_profile_created_not_the_one_requested(self):
        # MESA_GL_VERSION_OVERRIDE=4.6COMPAT turns a core request into a compatibility context
        self.assertEqual(gap.actual_api("core", "4.6 (Compatibility Profile) Mesa 26.1.8", None), "compat")
        self.assertEqual(gap.actual_api("core", "4.5 (Core Profile) Mesa 26.1.8", None), "core")
        self.assertEqual(gap.actual_api("core", "3.2.0 NVIDIA 595.91.07", 1), "core")
        self.assertEqual(gap.actual_api("compat", "4.6.0 NVIDIA 595.91.07", 2), "compat")
        self.assertEqual(gap.actual_api("compat", "2.1 Mesa 26.1.8", None), "compat")
        self.assertEqual(gap.actual_api("es", "OpenGL ES 3.2 Mesa 26.1.8", None), "es")

    def test_gl_version_number(self):
        self.assertEqual(gap.gl_version_number("2.1 Mesa 26.1.8"), 21)
        self.assertEqual(gap.gl_version_number("OpenGL ES 3.2 build 24.2@6603887"), 32)
        self.assertEqual(gap.gl_version_number("4.5 (Core Profile) Mesa 26.1.8"), 45)
        self.assertEqual(gap.gl_version_number("OpenGL ES GLSL ES 3.20"), 32)
        self.assertEqual(gap.gl_version_number("4.60"), 46)
        self.assertIsNone(gap.gl_version_number(""))
        self.assertIsNone(gap.gl_version_number(None))

    def test_glxinfo_versions_extensions_limits(self):
        g = gap.parse_glxinfo(GLXINFO)
        self.assertEqual(g["max_versions"], {"core": "0.0", "compat": "2.1", "gles1": "1.1", "gles": "2.0"})
        self.assertEqual(g["compat"]["renderer"], "zink Vulkan 1.2(PowerVR B-Series BXM-4-64 MC1 (IMAGINATION_OPEN_SOURCE_MESA))")
        self.assertEqual(g["compat"]["extensions"], ["GL_ARB_shadow", "GL_ARB_vertex_shader", "GL_EXT_texture_sRGB"])
        self.assertEqual(g["es"]["version"], "OpenGL ES 2.0 Mesa 26.1.8")
        self.assertEqual(g["es"]["extensions"], ["GL_OES_texture_float", "GL_EXT_sRGB"])
        self.assertEqual(g["limits"]["compat"]["GL_MAX_COLOR_ATTACHMENTS"], "4")
        self.assertEqual(g["limits"]["compat"]["GL_MAX_VIEWPORT_DIMS"], "4096, 4096")
        self.assertEqual(g["limits"]["es"]["GL_MAX_TEXTURE_SIZE"], "4096")

    def test_vulkan_normalized(self):
        v = gap.normalize_vulkan(VP)
        self.assertEqual(v["api_version"], "1.2.354")
        self.assertEqual(v["device_name"], "PowerVR B-Series BXM-4-64 MC1")
        self.assertEqual(v["device_type"], "VK_PHYSICAL_DEVICE_TYPE_INTEGRATED_GPU")
        self.assertEqual(v["driver_info"], "Mesa 26.1.8")
        self.assertIs(v["features"]["VkPhysicalDeviceFeatures.geometryShader"], False)
        self.assertIs(v["features"]["VkPhysicalDeviceVulkan12Features.timelineSemaphore"], True)
        self.assertEqual(v["limits"]["maxPerStageDescriptorUniformBuffers"], 12)
        self.assertEqual(v["extensions"]["VK_EXT_custom_border_color"], 12)
        self.assertEqual(v["formats"]["VK_FORMAT_R8G8B8A8_UNORM"]["optimal"],
                         ["VK_FORMAT_FEATURE_SAMPLED_IMAGE_BIT", "VK_FORMAT_FEATURE_COLOR_ATTACHMENT_BIT"])

    def test_glmark2_output(self):
        out = textwrap.dedent("""\
            =======================================================
                glmark2 2023.01
            =======================================================
                OpenGL Information
                GL_VENDOR:      Mesa
                GL_RENDERER:    zink Vulkan 1.2(PowerVR B-Series BXM-4-64 MC1 (IMAGINATION_OPEN_SOURCE_MESA))
                GL_VERSION:     OpenGL ES 2.0 Mesa 26.1.8
                Surface Config: buf=32 r=8 g=8 b=8 a=8 depth=24 stencil=0 samples=0
                Surface Size:   640x480 windowed
            =======================================================
            [build] use-vbo=false: FPS: 309 FrameTime: 3.236 ms
            [build] use-vbo=true: FPS: 320 FrameTime: 3.125 ms
            [texture] texture-filter=nearest: FPS: 394 FrameTime: 2.538 ms
            =======================================================
                                              glmark2 Score: 351
            =======================================================
            """)
        r = gap.parse_glmark2(out)
        self.assertEqual(r["renderer"], "zink Vulkan 1.2(PowerVR B-Series BXM-4-64 MC1 (IMAGINATION_OPEN_SOURCE_MESA))")
        self.assertEqual(r["version"], "OpenGL ES 2.0 Mesa 26.1.8")
        self.assertEqual(r["surface"], "640x480 windowed")
        self.assertEqual(r["score"], 351)
        self.assertEqual(r["scenes"], [["build", "use-vbo=false", 309], ["build", "use-vbo=true", 320],
                                       ["texture", "texture-filter=nearest", 394]])


class Gates(unittest.TestCase):
    def test_ext_table(self):
        t = gap.parse_ext_table(EXT_TABLE)
        self.assertEqual(t["ARB_shadow"], [{"name": "GL_ARB_shadow", "gll": 0, "glc": None, "es1": None, "es2": None}])
        self.assertEqual(t["ARB_gpu_shader5"][0]["gll"], 32)
        # two strings behind one driver flag
        self.assertEqual([e["name"] for e in t["OES_geometry_shader"]], ["GL_OES_geometry_shader", "GL_EXT_geometry_shader"])
        self.assertEqual(t["OES_texture_float"][0]["es2"], 0)

    def test_version_gates_extracted_in_order(self):
        g = gap.parse_version_gates(VERSION_C)
        self.assertEqual([s["step"] for s in g["gl"]], ["1.4", "1.5", "2.0", "3.0", "3.1"])
        self.assertEqual([s["step"] for s in g["es"]], ["2.0", "3.0", "3.1"])
        self.assertEqual(g["es"][1]["var"], "ver_3_0")
        self.assertIn("es31_compute_shader", g["es_defs"])
        # comments stripped, whitespace collapsed
        self.assertNotIn("comment", json.dumps(g))
        self.assertEqual(g["gl"][1]["expr"], "ver_1_4")

    def test_expression_evaluation_three_valued(self):
        g = gap.parse_version_gates(VERSION_C)
        flags = {"ARB_shadow": True, "ARB_vertex_shader": True, "ARB_color_buffer_float": True,
                 "EXT_transform_feedback": False, "ARB_uniform_buffer_object": None}
        consts = {"GLSLVersion": 460, "MaxSamples": 4, "FakeSWMSAA": False,
                  "Program[MESA_SHADER_VERTEX].MaxTextureImageUnits": 16, "MaxColorAttachments": 4}
        r = gap.evaluate_gates(g, flags, consts)
        gl = {s["step"]: s for s in r["gl_compat"]}
        self.assertEqual(gl["2.0"]["value"], True)
        self.assertEqual(gl["3.0"]["value"], False)
        self.assertEqual(gl["3.0"]["fails"], ["extensions->EXT_transform_feedback"])
        # the step's own terms pass, the step fails through the previous one
        self.assertEqual(gl["3.1"]["value"], False)
        self.assertEqual(gl["3.1"]["fails"], [])
        self.assertEqual(gl["3.1"]["blocked_by"], "3.0")
        es = {s["step"]: s for s in r["es"]}
        self.assertEqual(es["3.0"]["value"], False)
        self.assertEqual(es["3.0"]["fails"], ["extensions->EXT_transform_feedback"])
        self.assertEqual(es["3.0"]["unknown"], ["extensions->ARB_uniform_buffer_object"])
        self.assertEqual(r["reached"]["gl_compat"], "2.0")
        self.assertEqual(r["reached"]["es"], "2.0")

    def test_core_profile_takes_the_api_branch(self):
        g = gap.parse_version_gates(VERSION_C)
        flags = {"ARB_shadow": True, "ARB_vertex_shader": True, "ARB_color_buffer_float": False,
                 "EXT_transform_feedback": True}
        consts = {"GLSLVersion": 460, "MaxSamples": 4, "FakeSWMSAA": False,
                  "Program[MESA_SHADER_VERTEX].MaxTextureImageUnits": 16}
        r = gap.evaluate_gates(g, flags, consts)
        self.assertEqual({s["step"]: s["value"] for s in r["gl_compat"]}["3.0"], False)
        self.assertEqual({s["step"]: s["value"] for s in r["gl_core"]}["3.0"], True)
        self.assertEqual(r["reached"]["gl_core"], "3.1")

    def test_flag_observation_respects_api_and_version_gating(self):
        t = gap.parse_ext_table(EXT_TABLE)
        ctxs = [
            {"api": "compat", "version": 21, "extensions": ["GL_ARB_shadow"], "source": "compat 2.1"},
            {"api": "es", "version": 20, "extensions": ["GL_OES_texture_float"], "source": "es 2.0"},
        ]
        o = gap.observe_flags(t, ctxs)
        self.assertEqual(o["ARB_shadow"]["value"], True)
        # visible in a GL 2.1 compat context and absent there: really false
        self.assertEqual(o["EXT_transform_feedback"]["value"], False)
        # GL_ARB_gpu_shader5 needs a 3.2 compat context to be listed: absent at 2.1 proves nothing
        self.assertIsNone(o["ARB_gpu_shader5"]["value"])
        self.assertIsNone(o["OES_geometry_shader"]["value"])
        self.assertEqual(o["OES_texture_float"]["value"], True)
        # the override probe (a 3.2 ES context) makes geometry shaders observable
        ctxs.append({"api": "es", "version": 32, "extensions": ["GL_EXT_geometry_shader"], "source": "es 3.2 override"})
        o = gap.observe_flags(t, ctxs)
        self.assertEqual(o["OES_geometry_shader"]["value"], True)
        self.assertIn("es 3.2 override", o["OES_geometry_shader"]["evidence"])

    def test_software_device_platforms_do_not_count_for_the_gpu_view(self):
        # eglinfo's "Device #1" on the open stack is llvmpipe (driver swrast, EGL_MESA_device_software): its GL 4.5 /
        # ES 3.2 extension lists must not make the PowerVR view look like it has transform feedback
        view = {"eglinfo": gap.parse_eglinfo(EGLINFO)}
        self.assertFalse(any("Device #1" in c["source"] for c in gap.view_contexts(view)))
        versions, _ = gap.view_versions(view)
        self.assertEqual(versions["es"], 20)
        # a view where every platform is software (Fedora's Mesa on the DDK boot) keeps them
        sw = {"eglinfo": {"platforms": [p for p in gap.parse_eglinfo(EGLINFO)["platforms"] if p["name"] == "Device #1"]}}
        self.assertEqual(gap.view_versions(sw)[0]["es"], 32)
        # the same for glxinfo through Xwayland when it lands on llvmpipe while the GPU view does not
        glx = gap.parse_glxinfo(GLXINFO.replace("OpenGL renderer string: zink", "OpenGL renderer string: llvmpipe"))
        glx["compat"]["version"] = "4.5 Mesa 26.1.8"
        self.assertEqual(gap.view_versions({"eglinfo": view["eglinfo"], "glxinfo": glx})[0]["compat"], 21)

    def test_consts_from_probe(self):
        probe = {"contexts": [
            {"api": "compat", "label": "normal", "ok": True, "version": "2.1 Mesa 26.1.8", "sl_version": "1.20",
             "ints": {"GL_MAX_COLOR_ATTACHMENTS": [4], "GL_MAX_SAMPLES": [4], "GL_MAX_CUBE_MAP_TEXTURE_SIZE": [4096],
                      "GL_MAX_3D_TEXTURE_SIZE": [256], "GL_MAX_VERTEX_UNIFORM_BLOCKS": None}},
            {"api": "compat", "label": "override", "ok": True, "version": "4.6 (Compatibility Profile) Mesa 26.1.8",
             "sl_version": "4.60", "ints": {"GL_MAX_VERTEX_UNIFORM_BLOCKS": None, "GL_MAX_VERTEX_ATTRIB_STRIDE": [2048]}},
        ]}
        c = gap.consts_from_probe(probe)
        self.assertEqual(c["MaxColorAttachments"]["value"], 4)
        self.assertEqual(c["MaxCubeTextureLevels"]["value"], 13)      # 4096 = 2^12 -> 13 levels
        self.assertEqual(c["Max3DTextureLevels"]["value"], 9)         # 256 -> 9 levels
        self.assertEqual(c["MaxVertexAttribStride"]["value"], 2048)
        self.assertIn("override", c["MaxVertexAttribStride"]["evidence"])
        # the normal 2.1 context clamps the GLSL string to 1.20; the override context shows the internal level
        self.assertEqual(c["GLSLVersion"]["value"], 460)
        self.assertIsNone(c["Program[MESA_SHADER_VERTEX].MaxUniformBlocks"]["value"])

    @unittest.skipUnless(GATES.exists(), "gates JSON not generated yet")
    def test_generated_gates_file_is_the_real_mesa_list(self):
        g = json.loads(GATES.read_text())
        self.assertEqual(g["mesa_version"], "26.1.8")
        self.assertEqual([s["step"] for s in g["gates"]["gl"]][-1], "4.6")
        self.assertEqual([s["step"] for s in g["gates"]["es"]], ["2.0", "3.0", "3.1", "3.2"])
        es30 = g["gates"]["es"][1]["expr"]
        for flag in ("EXT_transform_feedback", "ARB_uniform_buffer_object", "OES_depth_texture_cube_map"):
            self.assertIn("extensions->" + flag, es30)
        for flag in gap.gate_flags(g["gates"]):
            self.assertIn(flag, g["ext_table"], flag)


class VulkanDiff(unittest.TestCase):
    def test_features_extensions_and_limit_directions(self):
        o = vk_view()
        d = vk_view(features={"VkPhysicalDeviceFeatures.geometryShader": True,
                              "VkPhysicalDeviceTransformFeedbackFeaturesEXT.transformFeedback": True},
                    extensions={"VK_EXT_transform_feedback": 1},
                    limits={"maxPerStageDescriptorUniformBuffers": 16, "maxImageDimension2D": 8192,
                            "minMemoryMapAlignment": 64,
                            "framebufferColorSampleCounts": ["VK_SAMPLE_COUNT_1_BIT", "VK_SAMPLE_COUNT_2_BIT",
                                                             "VK_SAMPLE_COUNT_4_BIT"],
                            "pointSizeRange": [1.0, 511.0], "timestampComputeAndGraphics": True})
        r = gap.diff_vulkan(o, d)
        self.assertIn("geometryShader", [f["member"] for f in r["features_missing"]])
        self.assertIn("transformFeedback", [f["member"] for f in r["features_missing"]])
        self.assertEqual(r["extensions_missing"], ["VK_EXT_transform_feedback"])
        worse = {x["name"]: x for x in r["limits_worse"]}
        self.assertEqual(worse["maxPerStageDescriptorUniformBuffers"]["open"], 12)
        self.assertEqual(worse["maxPerStageDescriptorUniformBuffers"]["vendor"], 16)
        self.assertIn("maxImageDimension2D", worse)
        self.assertIn("minMemoryMapAlignment", worse)          # a bigger minimum alignment is worse
        self.assertIn("framebufferColorSampleCounts", worse)   # fewer sample counts
        self.assertIn("timestampComputeAndGraphics", worse)
        self.assertNotIn("pointSizeRange", worse)             # identical

    def test_a_feature_the_open_driver_has_is_not_missing(self):
        o = vk_view(features={"VkPhysicalDeviceVulkan12Features.timelineSemaphore": True})
        d = vk_view(features={"VkPhysicalDeviceTimelineSemaphoreFeatures.timelineSemaphore": True})
        self.assertEqual(gap.diff_vulkan(o, d)["features_missing"], [])

    def test_requirements(self):
        o = vk_view()
        d = vk_view(extensions={"VK_EXT_transform_feedback": 1}, limits={"maxPerStageDescriptorUniformBuffers": 16},
                    features={"VkPhysicalDeviceFeatures.geometryShader": True})
        self.assertIs(gap.req_eval("ext:VK_EXT_transform_feedback", o), False)
        self.assertIs(gap.req_eval("ext:VK_EXT_transform_feedback", d), True)
        self.assertIs(gap.req_eval("feat:geometryShader", d), True)
        self.assertIs(gap.req_eval("feat:geometryShader", o), False)
        self.assertIs(gap.req_eval("feat:noSuchFeature", o), False)
        self.assertIs(gap.req_eval("limit:maxPerStageDescriptorUniformBuffers>=13", o), False)
        self.assertIs(gap.req_eval("limit:maxPerStageDescriptorUniformBuffers>=13", d), True)
        self.assertIs(gap.req_eval("limit:maxUniformBufferRange>=16384", o), True)
        self.assertIs(gap.req_eval("fmt:VK_FORMAT_R8G8B8A8_UNORM:optimal:VK_FORMAT_FEATURE_SAMPLED_IMAGE_BIT", o), True)
        self.assertIs(gap.req_eval("fmt:VK_FORMAT_BC1_RGB_UNORM_BLOCK:optimal:VK_FORMAT_FEATURE_SAMPLED_IMAGE_BIT", o), False)
        self.assertIsNone(gap.req_eval("limit:maxPerStageDescriptorUniformBuffers>=13", None))
        with self.assertRaises(ValueError):
            gap.req_eval("bogus:thing", o)

    def test_requirement_alternatives_bits_queue_families_and_sample_counts(self):
        o = vk_view(queue_families=[{"VkQueueFamilyProperties": {"queueFlags": ["VK_QUEUE_GRAPHICS_BIT"],
                                                                  "timestampValidBits": 0}}],
                    properties={"VkPhysicalDeviceVulkan11Properties.subgroupSupportedOperations": ["VK_SUBGROUP_FEATURE_BASIC_BIT"]},
                    limits={"framebufferNoAttachmentsSampleCounts": ["VK_SAMPLE_COUNT_1_BIT", "VK_SAMPLE_COUNT_4_BIT"]})
        d = vk_view(queue_families=[{"VkQueueFamilyProperties": {"queueFlags": ["VK_QUEUE_TRANSFER_BIT"], "timestampValidBits": 0}},
                                    {"VkQueueFamilyProperties": {"queueFlags": ["VK_QUEUE_GRAPHICS_BIT"], "timestampValidBits": 64}}],
                    properties={"VkPhysicalDeviceSubgroupProperties.supportedOperations": ["VK_SUBGROUP_FEATURE_BASIC_BIT",
                                                                                          "VK_SUBGROUP_FEATURE_VOTE_BIT"]})
        # timestampValidBits lives in the first graphics queue family, not in VkPhysicalDeviceLimits
        self.assertIs(gap.req_eval("limit:timestampValidBits>=1", o), False)
        self.assertIs(gap.req_eval("limit:timestampValidBits>=1", d), True)
        # a list of VK_SAMPLE_COUNT_n_BIT compares by its highest count
        self.assertIs(gap.req_eval("limit:framebufferNoAttachmentsSampleCounts>=4", o), True)
        self.assertIs(gap.req_eval("limit:framebufferNoAttachmentsSampleCounts>=8", o), False)
        # bit tests on flag lists, any struct
        self.assertIs(gap.req_eval("bit:supportedOperations:VK_SUBGROUP_FEATURE_VOTE_BIT", d), True)
        self.assertIs(gap.req_eval("bit:subgroupSupportedOperations:VK_SUBGROUP_FEATURE_VOTE_BIT", o), False)
        # "a|b": either
        self.assertIs(gap.req_eval("feat:geometryShader|ext:VK_EXT_custom_border_color", o), True)
        self.assertIs(gap.req_eval("feat:geometryShader|ext:VK_EXT_transform_feedback", o), False)
        self.assertIsNone(gap.req_eval("feat:geometryShader|ext:VK_EXT_transform_feedback", None))
        with self.assertRaises(ValueError):
            gap.req_eval("feat:geometryShader|nonsense", o)


class Ranking(unittest.TestCase):
    NEEDS = {
        "EXT_transform_feedback": {"needs": ["ext:VK_EXT_transform_feedback", "feat:transformFeedback"],
                                   "class": "project", "size": "weeks", "source": "s", "ddk_gles": "es:3.0"},
        "ARB_uniform_buffer_object": {"needs": ["limit:maxPerStageDescriptorUniformBuffers>=13",
                                                "limit:maxUniformBufferRange>=16384"],
                                      "class": "limit", "size": "hours", "source": "s", "ddk_gles": "es:3.0"},
    }
    FLAGS = {"ARB_shadow": True, "ARB_vertex_shader": True, "ARB_color_buffer_float": True,
             "EXT_transform_feedback": False, "ARB_uniform_buffer_object": False}
    CONSTS = {"GLSLVersion": 460, "MaxSamples": 4, "FakeSWMSAA": False, "MaxColorAttachments": 4,
              "Program[MESA_SHADER_VERTEX].MaxTextureImageUnits": 16, "MaxComputeWorkGroupInvocations": 128,
              "Program[MESA_SHADER_COMPUTE].MaxShaderStorageBlocks": 4}

    # ES 3.0 unlocks both, GL 3.0 only the second: ES 3.0 must come first although it costs one item more
    APPS = [{"name": "gtk-gl", "needs": ["gl:3.3", "es:3.0"]}, {"name": "webrender", "needs": ["gl:3.0", "es:3.0"]}]

    def rank(self, vvk, vendor_versions, vendor_exts=(), vendor_ints=None):
        g = gap.parse_version_gates(VERSION_C)
        return gap.rank_actions(g, dict(self.FLAGS), dict(self.CONSTS), vk_view(), vvk, vendor_versions,
                                list(vendor_exts), vendor_ints, needs=self.NEEDS, const_needs={}, apps=self.APPS)

    def test_milestones_say_what_they_unlock(self):
        vvk = vk_view(extensions={"VK_EXT_transform_feedback": 1}, limits={"maxPerStageDescriptorUniformBuffers": 16},
                      features={"VkPhysicalDeviceTransformFeedbackFeaturesEXT.transformFeedback": True})
        r = self.rank(vvk, {"es": 32})
        self.assertEqual(sorted(r["milestones"][0]["unlocks"]), ["gtk-gl", "webrender"])
        self.assertTrue(r["milestones"][0]["complete"])

    def test_milestones_list_vulkan_items_cheapest_first_with_cumulative_versions(self):
        vvk = vk_view(extensions={"VK_EXT_transform_feedback": 1}, limits={"maxPerStageDescriptorUniformBuffers": 16},
                      features={"VkPhysicalDeviceTransformFeedbackFeaturesEXT.transformFeedback": True})
        r = self.rank(vvk, {"es": 32, "compat": None, "core": None})
        first = r["milestones"][0]
        self.assertEqual(first["step"], "OpenGL ES 3.0")
        self.assertEqual([i["req"] for i in first["items"]],
                         ["limit:maxPerStageDescriptorUniformBuffers>=13", "ext:VK_EXT_transform_feedback",
                          "feat:transformFeedback"])
        ubo, xfb = first["items"][0], first["items"][1]
        self.assertEqual(ubo["class"], "limit")
        self.assertIs(ubo["vendor_vulkan"], True)
        # the limit alone does not move ES: transform feedback still blocks it
        self.assertEqual(ubo["reached_after"]["es"], "2.0")
        self.assertEqual(xfb["class"], "project")
        self.assertEqual(first["items"][-1]["reached_after"]["es"], "3.1")     # ES 3.1's own terms already pass
        self.assertEqual(first["items"][-1]["reached_after"]["gl_compat"], "3.1")
        self.assertIn("EXT_transform_feedback", xfb["terms"])
        # the requirement the open driver already meets is not an item
        self.assertNotIn("limit:maxUniformBufferRange>=16384", [i["req"] for m in r["milestones"] for i in m["items"]])
        # no item is listed twice across milestones
        reqs = [i["req"] for m in r["milestones"] for i in m["items"]]
        self.assertEqual(len(reqs), len(set(reqs)))

    @unittest.skipUnless(GATES.exists(), "gates JSON not generated yet")
    def test_milestones_advance_one_step_at_a_time_per_api(self):
        # real Mesa gates; everything passes except four flags. An app that needs GL 4.0 must not make the ranking
        # jump to GL 4.0 and lump GL 3.0-4.0 into one milestone: each round offers only the lowest missing step of
        # each API
        g = json.loads(GATES.read_text())["gates"]
        missing = {"EXT_transform_feedback": ["ext:VK_EXT_transform_feedback"],
                   "ARB_uniform_buffer_object": ["limit:maxPerStageDescriptorUniformBuffers>=13"],
                   "ARB_blend_func_extended": ["feat:dualSrcBlend"],
                   "ARB_tessellation_shader": ["feat:tessellationShader"]}
        flags = {f: f not in missing for f in gap.gate_flags(g)}
        consts = {"GLSLVersion": 460, "MaxSamples": 4, "FakeSWMSAA": False, "MaxColorAttachments": 8,
                  "Program[MESA_SHADER_VERTEX].MaxTextureImageUnits": 16, "MaxTextureSize": 16384,
                  "MaxRenderbufferSize": 16384, "MaxCubeTextureLevels": 15, "Max3DTextureLevels": 12,
                  "MaxArrayTextureLayers": 2048, "Program[MESA_SHADER_VERTEX].MaxUniformBlocks": 14,
                  "MaxVertexAttribStride": 2048, "MaxComputeWorkGroupInvocations": 1024,
                  "Program[MESA_SHADER_COMPUTE].MaxShaderStorageBlocks": 8,
                  "Program[MESA_SHADER_COMPUTE].MaxAtomicBuffers": 8,
                  "Program[MESA_SHADER_COMPUTE].MaxImageUniforms": 8, "PrimitiveRestartFixedIndex": True}
        needs = {f: {"needs": r, "class": "project", "size": "", "source": "s"} for f, r in missing.items()}
        vvk = vk_view(extensions={"VK_EXT_transform_feedback": 1}, limits={"maxPerStageDescriptorUniformBuffers": 16},
                      features={"VkPhysicalDeviceFeatures.dualSrcBlend": True,
                                "VkPhysicalDeviceFeatures.tessellationShader": True})
        r = gap.rank_actions(g, flags, consts, vk_view(), vvk, {"es": 32}, [], needs=needs, const_needs={},
                             apps=[{"name": "gl4-app", "needs": ["gl:4.0"]}])
        self.assertLessEqual(gap.gl_version_number(r["milestones"][0]["version"]), 30)
        for key in ("es", "gl_compat"):
            vs = [gap.gl_version_number(m["version"]) for m in r["milestones"] if m["key"] == key]
            self.assertEqual(vs, sorted(vs), key)
        self.assertTrue(all(m["complete"] for m in r["milestones"]))
        self.assertIn("gl4-app", [a for m in r["milestones"] for a in m["unlocks"]])
        self.assertEqual(r["milestones"][-1]["reached"]["gl_compat"], "4.6")

    def test_vendor_gles_is_evidence_when_the_vendor_vulkan_lacks_the_item(self):
        vvk = vk_view(limits={"maxPerStageDescriptorUniformBuffers": 16})      # DDK Vulkan without XFB
        xfb = [i for m in self.rank(vvk, {"es": 32})["milestones"] for i in m["items"]
               if i["req"] == "ext:VK_EXT_transform_feedback"][0]
        self.assertIs(xfb["vendor_vulkan"], False)
        self.assertEqual(xfb["class"], "project")
        self.assertIn("es:3.0", xfb["vendor_gles"])

    def test_hardware_absent_when_the_vendor_has_it_nowhere(self):
        vvk = vk_view()                                   # DDK Vulkan: 12 UBOs, no XFB, and only GLES 2.0
        # an ES 2.0 context rejects the uniform-block queries: that is a disproof; a missing number would be unknown
        rejected = {"GL_MAX_VERTEX_UNIFORM_BLOCKS": None, "GL_MAX_UNIFORM_BLOCK_SIZE": None}
        items = {i["req"]: i for m in self.rank(vvk, {"es": 20}, vendor_ints=rejected)["milestones"] for i in m["items"]}
        self.assertEqual(items["ext:VK_EXT_transform_feedback"]["class"], "hardware")
        self.assertEqual(items["limit:maxPerStageDescriptorUniformBuffers>=13"]["class"], "hardware")

    def test_gles_evidence(self):
        self.assertIs(gap.gles_evidence("es:3.0", {"es": 32}, []), True)
        self.assertIs(gap.gles_evidence("es:3.2|glesext:GL_EXT_geometry_shader", {"es": 31}, ["GL_EXT_geometry_shader"]), True)
        self.assertIs(gap.gles_evidence("glesext:GL_EXT_geometry_shader", {"es": 32}, []), False)
        self.assertIsNone(gap.gles_evidence(None, {"es": 32}, []))
        # a GLES limit of the vendor's ES context: proves what an extension list cannot (fragment SSBOs, 1024
        # compute invocations, 16384 textures)
        ints = {"GL_MAX_FRAGMENT_SHADER_STORAGE_BLOCKS": [4], "GL_MAX_TEXTURE_SIZE": [8192], "GL_MAX_VIEWPORTS": None}
        self.assertIs(gap.gles_evidence("glesint:GL_MAX_FRAGMENT_SHADER_STORAGE_BLOCKS>=1", {"es": 32}, [], ints), True)
        self.assertIs(gap.gles_evidence("glesint:GL_MAX_TEXTURE_SIZE>=16384", {"es": 32}, [], ints), False)
        self.assertIs(gap.gles_evidence("glesint:GL_MAX_VIEWPORTS>=16", {"es": 32}, [], ints), False)
        # no limits recorded at all (the vendor probe failed): unknown, not absent
        self.assertIsNone(gap.gles_evidence("glesint:GL_MAX_TEXTURE_SIZE>=4096|glesext:GL_X", {"es": 32}, [], None))
        with self.assertRaises(ValueError):
            gap.gles_evidence("glesint:GL_MAX_TEXTURE_SIZE", {"es": 32}, [], ints)


class Knowledge(unittest.TestCase):
    def test_every_curated_requirement_parses(self):
        o = vk_view()
        for flag, k in gap.GL_FLAG_NEEDS.items():
            for req in k["needs"]:
                gap.req_eval(req, o)          # raises on a malformed requirement
            self.assertIn(k["class"], ("limit", "project", "hardware", "mesa"), flag)
            self.assertTrue(k.get("source"), flag)

    def test_curated_flags_exist_in_mesa(self):
        if not GATES.exists():
            self.skipTest("gates JSON not generated yet")
        g = json.loads(GATES.read_text())
        for flag in gap.GL_FLAG_NEEDS:
            self.assertIn(flag, g["ext_table"], flag)

    def test_app_table_shape(self):
        for a in gap.APPS:
            self.assertTrue(a["name"] and a["source"], a)
            for need in a["needs"]:
                gap.parse_need(need)        # raises on a malformed entry
        with self.assertRaises(ValueError):
            gap.parse_need("gl:three")
        with self.assertRaises(ValueError):
            gap.parse_need("vk:1.2+bogus:x")

    def test_app_needs_are_alternatives_of_conjunctions(self):
        vk = vk_view(features={"VkPhysicalDeviceVulkan12Features.timelineSemaphore": True})
        v = {"compat": 21, "core": None, "es": 20}
        self.assertTrue(gap.app_meets(["gl:2.0"], v, vk))
        self.assertFalse(gap.app_meets(["gl:3.3", "es:3.0"], v, vk))
        self.assertTrue(gap.app_meets(["gl:3.0", "vk:1.2+vkfeat:timelineSemaphore"], v, vk))
        self.assertFalse(gap.app_meets(["vk:1.2+vkfeat:geometryShader"], v, vk))
        self.assertTrue(gap.app_meets(["vk:1.2+vkext:VK_EXT_custom_border_color"], v, vk))
        self.assertFalse(gap.app_meets(["vk:1.3"], v, vk))
        self.assertFalse(gap.app_meets(["glcore:3.2"], {"compat": 46, "core": None, "es": 32}, vk))
        self.assertFalse(gap.app_meets(["vk:1.0"], v, None))


class Collect(unittest.TestCase):
    """snapshot collection with a fake command runner and a fake file reader"""

    def fake_runner(self, calls):
        vp = json.dumps(VP)

        def run(cmd, env=None, cwd=None, timeout=60):
            calls.append((cmd, dict(env or {})))
            exe = os.path.basename(cmd[0])
            if exe == "vulkaninfo" and "--summary" in cmd:
                return 0, "GPU0:\n\tdeviceType = PHYSICAL_DEVICE_TYPE_INTEGRATED_GPU\n\tdeviceName = PowerVR\nGPU1:\n\tdeviceType = PHYSICAL_DEVICE_TYPE_CPU\n\tdeviceName = llvmpipe\n", ""
            if exe == "vulkaninfo":
                Path(cwd, "VP_VULKANINFO_PowerVR_26_1_8.json").write_text(vp)
                return 0, "", ""
            if exe == "eglinfo":
                return 0, EGLINFO, ""
            if exe == "glxinfo":
                return 0, GLXINFO, ""
            if cmd[1:2] == [str(SCRIPT)] or (len(cmd) > 2 and cmd[2] == "probe"):
                return 0, json.dumps({"contexts": [{"api": "compat", "label": "normal", "ok": True,
                                                    "version": "2.1 Mesa 26.1.8", "sl_version": "1.20",
                                                    "extensions": ["GL_ARB_shadow"], "ints": {}}]}), ""
            return 127, "", "not faked: %s" % cmd

        return run

    def test_view_records_every_tool_and_the_env(self):
        calls = []
        with tempfile.TemporaryDirectory() as t:
            v = gap.collect_view("open", {"A": "1"}, self.fake_runner(calls), session={"DISPLAY": ":0"}, tmp=t)
        self.assertEqual(v["vulkan"]["device_name"], "PowerVR B-Series BXM-4-64 MC1")
        self.assertEqual(v["vulkan_gpu"], 0)                  # the non-CPU device from --summary
        self.assertEqual([p["name"] for p in v["eglinfo"]["platforms"]][0], "GBM")
        self.assertEqual(v["glxinfo"]["max_versions"]["compat"], "2.1")
        self.assertEqual(v["probe"]["normal"]["contexts"][0]["version"], "2.1 Mesa 26.1.8")
        self.assertIn("override", v["probe"])
        self.assertEqual(v["env"], {"A": "1"})
        # the override probe is the only call with a version override in its environment
        overridden = [c for c, e in calls if "MESA_GL_VERSION_OVERRIDE" in e]
        self.assertEqual(len(overridden), 1)
        self.assertIn("probe", overridden[0])

    def test_view_without_session_skips_glxinfo(self):
        calls = []
        with tempfile.TemporaryDirectory() as t:
            v = gap.collect_view("open", {}, self.fake_runner(calls), session=None, tmp=t)
        self.assertIn("no X session", v["glxinfo"]["error"])
        self.assertFalse(any(os.path.basename(c[0]) == "glxinfo" for c, _ in calls))

    def test_view_without_override_probe_for_the_ddk(self):
        calls = []
        with tempfile.TemporaryDirectory() as t:
            v = gap.collect_view("ddk", {}, self.fake_runner(calls), session=None, tmp=t, override_probe=False)
        self.assertNotIn("override", v["probe"])

    def test_a_malformed_vulkan_profile_is_recorded_not_fatal(self):
        base = self.fake_runner([])

        def run(cmd, env=None, cwd=None, timeout=60):
            if os.path.basename(cmd[0]) == "vulkaninfo" and "--summary" not in cmd:
                Path(cwd, "VP_VULKANINFO_x.json").write_text('{"profiles": {}}')      # no "capabilities"
                return 0, "", ""
            return base(cmd, env, cwd, timeout)

        with tempfile.TemporaryDirectory() as t:
            v = gap.collect_view("ddk", {}, run, session=None, tmp=t)
        self.assertIn("error", v["vulkan"])
        self.assertEqual(v["eglinfo"]["platforms"][0]["name"], "GBM")     # the rest of the view is still there

    def test_snapshot_writes_the_views_even_when_the_bench_fails(self):
        saved = {k: getattr(gap, k) for k in ("collect_system", "session_env", "collect_view", "collect_gtk4",
                                              "collect_bench")}

        def boom(*a, **k):
            raise RuntimeError("glmark2 exploded")

        def view_boom(name, *a, **k):
            if name == "fedora-mesa":
                raise RuntimeError("view exploded")
            return {"view": name}

        try:
            gap.collect_system = lambda *a, **k: {"stack": "ddk", "temps_c": {}}
            gap.session_env = lambda *a, **k: {"WAYLAND_DISPLAY": "wayland-0"}
            gap.collect_view = view_boom
            gap.collect_gtk4 = lambda *a, **k: {"renderer": "x"}
            gap.collect_bench = boom
            with tempfile.TemporaryDirectory() as t:
                out = Path(t) / "snap.json"
                args = gap.main.__globals__["argparse"].Namespace(out=str(out), views="ddk,fedora-mesa", bench=True,
                                                                  bench_full=False, reps=1, sizes="640x480")
                gap.snapshot(args)
                s = json.loads(out.read_text())
        finally:
            for k, f in saved.items():
                setattr(gap, k, f)
        self.assertEqual(s["views"]["ddk"]["view"], "ddk")
        self.assertIn("view exploded", s["views"]["fedora-mesa"]["error"])
        self.assertIn("glmark2 exploded", s["bench"]["error"])

    def test_gtk4_renderer_probe(self):
        calls = []

        def run(cmd, env=None, cwd=None, timeout=60):
            calls.append((cmd, dict(env or {})))
            return 0, 'Gtk-Message: whatever\n{"gtk": "4.22.4", "renderer": "GskVulkanRenderer"}\n', ""

        r = gap.collect_gtk4({"VK_ICD_FILENAMES": "x"}, {"WAYLAND_DISPLAY": "wayland-0"}, run)
        self.assertEqual(r, {"gtk": "4.22.4", "renderer": "GskVulkanRenderer"})
        self.assertEqual(calls[0][1], {"VK_ICD_FILENAMES": "x", "WAYLAND_DISPLAY": "wayland-0"})
        self.assertIn("get_renderer", calls[0][0][-1])          # the script it runs asks GTK, it does not guess
        self.assertIn("no desktop session", gap.collect_gtk4({}, None, run)["error"])
        r = gap.collect_gtk4({}, {"WAYLAND_DISPLAY": "wayland-0"}, lambda *a, **k: (1, "", "ImportError: gi"))
        self.assertIn("ImportError", r["error"])

    def test_system_facts_from_sysfs(self):
        files = {
            "/sys/bus/platform/devices/1800000.gpu/driver": "->powervr",
            "/sys/bus/platform/devices/1800000.gpu/power/control": "auto\n",
            "/sys/bus/platform/devices/1800000.gpu/power/runtime_status": "suspended\n",
            "/sys/class/regulator/regulator.7/name": "dcdc4\n",
            "/sys/class/regulator/regulator.7/microvolts": "800000\n",
            "/sys/class/regulator/regulator.3/name": "dcdc1\n",
            "/sys/class/regulator/regulator.3/microvolts": "900000\n",
        }

        def read(path):
            return files.get(path)

        def run(cmd, env=None, cwd=None, timeout=60):
            if cmd[:2] == ["sudo", "-n"] and "clk_summary" in cmd[-1]:
                return 0, "   pll-gpu      1 1 0 1008000000 0 0 50000 Y\n     gpu0     1 1 0 1008000000 0 0 50000 Y\n", ""
            if cmd[0] == "uname":
                return 0, "6.6.98-a7apvr.202609091821\n", ""
            if cmd[0] == "rpm":
                return 0, "mesa-pvr-a7a-26.1.8-202609091222.aarch64\n", ""
            return 1, "", ""

        s = gap.collect_system(run, read, listdir=lambda d: sorted({p[len(d) + 1:].split("/")[0] for p in files if p.startswith(d + "/")}))
        self.assertEqual(s["driver"], "powervr")
        self.assertEqual(s["stack"], "open")
        self.assertEqual(s["gpu_clock_mhz"], 1008)
        self.assertEqual(s["pll_gpu_mhz"], 1008)
        self.assertEqual(s["dcdc4_mv"], 800)
        self.assertEqual(s["runtime_pm"], {"control": "auto", "status": "suspended"})
        self.assertEqual(s["kernel"], "6.6.98-a7apvr.202609091821")


class Report(unittest.TestCase):
    def snapshot(self, stack, view, vk, compat, es, probe_extra=None):
        eg = {"platforms": [{"name": "Surfaceless", "error": None, "egl": {"driver": "powervr"},
                             "gl": {k: v for k, v in (("compat", compat), ("es", es)) if v}}]}
        return {"schema": 1, "stack": stack, "system": {"stack": stack, "kernel": "k", "driver": "d",
                                                       "gpu_clock_mhz": 1008, "dcdc4_mv": 800},
                "views": {view: {"vulkan": vk, "eglinfo": eg, "probe": probe_extra or {}}}}

    def test_report_names_the_failing_gate_and_the_vulkan_item_behind_it(self):
        if not GATES.exists():
            self.skipTest("gates JSON not generated yet")
        gates = json.loads(GATES.read_text())
        open_vk = vk_view()
        ddk_vk = vk_view(extensions={"VK_EXT_transform_feedback": 1}, limits={"maxPerStageDescriptorUniformBuffers": 16},
                         features={"VkPhysicalDeviceTransformFeedbackFeaturesEXT.transformFeedback": True})
        o = self.snapshot("open", "open", open_vk,
                          {"version": "2.1 Mesa 26.1.8", "extensions": ["GL_ARB_shadow"]},
                          {"version": "OpenGL ES 2.0 Mesa 26.1.8", "extensions": []})
        d = self.snapshot("ddk", "ddk", ddk_vk, None,
                          {"version": "OpenGL ES 3.2 build 24.2@6603887", "extensions": ["GL_EXT_geometry_shader"]})
        md = gap.render_report(o, d, gates, title="GPU gap test")
        self.assertIn("# GPU gap test", md)
        self.assertIn("VK_EXT_transform_feedback", md)
        self.assertIn("maxPerStageDescriptorUniformBuffers", md)
        self.assertIn("OpenGL ES 3.0", md)
        self.assertIn("EXT_transform_feedback", md)
        self.assertIn("OpenGL ES 3.2", md)          # the DDK's own version is reported


class Envelope(unittest.TestCase):
    @unittest.skipUnless(GATES.exists(), "gates JSON not generated yet")
    def test_envelope_states_the_vendor_to_open_ratio_per_client_and_size(self):
        gates = json.loads(GATES.read_text())

        def snap(stack, view, es, off, tex, full, extra_client=None):
            eg = {"platforms": [{"name": "Surfaceless", "error": None, "egl": {"driver": "x"},
                                 "gl": {"es": {"renderer": "PowerVR", "version": es, "extensions": []}}}]}
            clients = {"offscreen": {"640x480": {"median_fps": {"build": off, "texture": tex}, "renderer": "PowerVR"}}}
            if extra_client:
                clients[extra_client] = {"640x480": {"median_fps": {"build": 1, "texture": 1}, "renderer": "PowerVR"}}
            return {"schema": 1, "stack": stack, "system": {"stack": stack},
                    "views": {view: {"vulkan": vk_view(), "eglinfo": eg, "probe": {}}},
                    "bench": {"reps": 3, "sizes": ["640x480"], "clients": clients, "full": {"offscreen": {"score": full}}}}

        o = snap("open", "open", "OpenGL ES 2.0 Mesa 26.1.8", 473, 658, 300, extra_client="wayland")
        d = snap("ddk", "ddk", "OpenGL ES 3.2 build 24.2", 2155, 1846, 1586)
        md = gap.render_report(o, d, gates)
        self.assertIn("offscreen 640x480: build 4.6x, texture 2.8x", md)
        self.assertIn("whole suite offscreen: 5.3x", md)
        self.assertNotIn("wayland 640x480:", md)          # a client only one stack ran has no ratio


class CLI(unittest.TestCase):
    def test_help_lists_the_modes(self):
        r = subprocess.run([str(SCRIPT), "--help"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        for mode in ("snapshot", "report", "gates"):
            self.assertIn(mode, r.stdout)

    def test_gates_mode_writes_the_json(self):
        with tempfile.TemporaryDirectory() as t:
            src = Path(t) / "mesa"
            (src / "src/mesa/main").mkdir(parents=True)
            (src / "src/mesa/main/version.c").write_text(VERSION_C)
            (src / "src/mesa/main/extensions_table.h").write_text(EXT_TABLE)
            (src / "VERSION").write_text("26.1.8\n")
            out = Path(t) / "gates.json"
            r = subprocess.run([str(SCRIPT), "gates", "--mesa-src", str(src), "--out", str(out)],
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            g = json.loads(out.read_text())
            self.assertEqual(g["mesa_version"], "26.1.8")
            self.assertEqual(len(g["version_c_sha256"]), 64)
            self.assertIn("EXT_transform_feedback", g["ext_table"])
            self.assertNotIn("OES_geometry_shader", g["ext_table"])   # only the flags the gates use


class ReviewFixes(unittest.TestCase):
    """the first reviewer pass (2026-09-10) over the snapshot path, before the one-shot vendor boot"""

    def test_probe_output_is_one_json_line_per_context_and_survives_noise_and_crashes(self):
        out = ('{"platform": "surfaceless", "egl": {"vendor": "Imagination Technologies"}}\n'
               'libpvr: chatter on stdout\n'
               '{"context": {"api": "es", "ok": true, "version": "OpenGL ES 3.2 build 24.2@6603887", '
               '"extensions": ["GL_EXT_geometry_shader"], "ints": {"GL_MAX_TEXTURE_SIZE": [8192]}}}\n'
               'Segmentation fault\n')
        p = gap.parse_probe_output(out, -11, "")
        self.assertEqual(p["egl"]["vendor"], "Imagination Technologies")
        self.assertEqual([c["api"] for c in p["contexts"]], ["es"])
        self.assertIn("rc=-11", p["error"])
        done = out.replace("Segmentation fault\n", '{"done": true}\n')
        self.assertNotIn("error", gap.parse_probe_output(done, 0, ""))
        self.assertEqual(gap.parse_probe_output("", 1, "boom")["contexts"], [])
        self.assertIn("boom", gap.parse_probe_output("", 1, "boom")["error"])

    def test_the_probe_asks_for_gles_first(self):
        # a GLES-only vendor driver that dies on a desktop-GL request must still leave its ES context behind
        src = Path(SCRIPT).read_text()
        specs = src[src.index("specs = ["):src.index("for api, bind, attempts in specs")]
        self.assertLess(specs.index('("es"'), specs.index('("compat"'))
        self.assertNotIn("eglTerminate(dpy)", src[src.index("def probe_main"):src.index("# ---------", src.index("def probe_main"))])

    def test_real_probe_on_this_machine(self):
        r = subprocess.run([str(SCRIPT), "probe"], capture_output=True, text=True, timeout=60)
        p = gap.parse_probe_output(r.stdout, r.returncode, r.stderr)
        if not any(c.get("ok") for c in p["contexts"]):
            self.skipTest("no EGL surfaceless context on this machine: %s" % p.get("error"))
        self.assertNotIn("error", p)
        self.assertTrue(all(c["api"] in ("es", "compat", "core") for c in p["contexts"]))

    def test_session_env_comes_from_kwins_own_command_line(self):
        cmd = "\0".join(["/usr/bin/kwin_wayland", "--wayland-fd", "7", "--socket", "wayland-1", "--xwayland-display",
                         ":1", "--xwayland-xauthority", "/run/user/1000/xauth_ZqLive", "--xwayland"]) + "\0"
        files = {"/proc/42/status": "Name:\tkwin_wayland\nUid:\t1000\t1000\t1000\t1000\n", "/proc/42/cmdline": cmd}
        env = gap.session_env(run=lambda *a, **k: (0, "42\n", ""), read=files.get,
                              listdir=lambda d: ["xauth_AbStale", "xauth_ZqLive"])
        self.assertEqual(env, {"XDG_RUNTIME_DIR": "/run/user/1000", "WAYLAND_DISPLAY": "wayland-1", "DISPLAY": ":1",
                               "XAUTHORITY": "/run/user/1000/xauth_ZqLive"})
        # without the arguments: the only xauth file, else none at all (never an alphabetical guess)
        files["/proc/42/cmdline"] = "/usr/bin/kwin_wayland\0"
        one = gap.session_env(run=lambda *a, **k: (0, "42\n", ""), read=files.get, listdir=lambda d: ["xauth_Only"])
        self.assertEqual(one["XAUTHORITY"], "/run/user/1000/xauth_Only")
        self.assertEqual(one["WAYLAND_DISPLAY"], "wayland-0")
        two = gap.session_env(run=lambda *a, **k: (0, "42\n", ""), read=files.get, listdir=lambda d: ["xauth_A", "xauth_B"])
        self.assertNotIn("XAUTHORITY", two)

    def test_run_cmd_never_raises_and_never_hangs(self):
        with tempfile.TemporaryDirectory() as t:
            f = Path(t) / "notexec"
            f.write_text("#!/bin/sh\n")
            rc, out, err = gap.run_cmd([str(f)])
            self.assertNotEqual(rc, 0)
            self.assertIn("PermissionError", err)
        import time as _t
        t0 = _t.time()
        rc, out, err = gap.run_cmd(["sleep", "10"], timeout=0.5)
        self.assertEqual(rc, 124)
        self.assertLess(_t.time() - t0, 5)

    def test_zink_on_ddk_library_set_is_the_icd_and_its_dependencies_only(self):
        with tempfile.TemporaryDirectory() as t:
            lib = Path(t) / "opt-pvr-lib"
            lib.mkdir()
            for n in ("libVK_IMG.so", "libVK_IMG.so.1", "libsrv_um.so.24.2.6603887", "libusc.so",
                      "libopencv_core.so.410", "libEGL.so.1", "libvulkan.so.1"):
                (lib / n).write_text("")
            icd = Path(t) / "powervr_icd.json"
            icd.write_text(json.dumps({"file_format_version": "1.0.0", "ICD": {"library_path": "libVK_IMG.so.1"}}))
            for n in ("libPVRScopeServices.so", "libdrm.so.2"):
                (lib / n).write_text("")
            sysdir = Path(t) / "usr-lib64"
            sysdir.mkdir()
            (sysdir / "libdrm.so.2").write_text("")                   # Fedora has its own: never shadow it
            ldd = ("\tlinux-vdso.so.1 (0x0000ffff9c0a0000)\n"
                   "\tlibsrv_um.so => %s/libsrv_um.so.24.2.6603887 (0x0000ffff9b000000)\n"
                   "\tlibusc.so => %s/libusc.so (0x0000ffff9a000000)\n"
                   "\tlibdrm.so.2 => %s/libdrm.so.2 (0x0000ffff98000000)\n"
                   "\tlibc.so.6 => /lib64/libc.so.6 (0x0000ffff99000000)\n" % (lib, lib, lib))
            # what the ICD loads at run time (dlopen) only shows in the loader's own trace
            ld_debug = ("      4711:\tfile=libPVRScopeServices.so [0];  dynamically loaded by %s/libVK_IMG.so.1 [0]\n"
                        "      4711:\tfile=libdrm.so.2 [0];  needed by %s/libVK_IMG.so.1 [0]\n"
                        "      4711:\tfile=libX11.so.6 [0];  needed by vulkaninfo [0]\n" % (lib, lib))
            calls = []

            def run(cmd, env=None, cwd=None, timeout=60):
                calls.append((cmd, env))
                if cmd[0] == "ldd":
                    return 0, ldd, ""
                return 0, "", ld_debug

            env = gap.ddk_vulkan_only_env(t, libdir=str(lib), icd=str(icd), run=run, sysdirs=(str(sysdir),))
            farm = sorted(os.listdir(env["LD_LIBRARY_PATH"]))
            self.assertEqual(farm, ["libPVRScopeServices.so", "libVK_IMG.so.1", "libsrv_um.so", "libusc.so"])
            self.assertEqual(os.readlink(Path(env["LD_LIBRARY_PATH"]) / "libsrv_um.so"),
                             str(lib / "libsrv_um.so.24.2.6603887"))
            self.assertEqual(calls[0][0], ["ldd", str(lib / "libVK_IMG.so.1")])
            self.assertEqual(calls[0][1]["LD_LIBRARY_PATH"], str(lib))
            trace = [e for c, e in calls if c[0] == "vulkaninfo"][0]
            self.assertEqual((trace["LD_DEBUG"], trace["VK_ICD_FILENAMES"], trace["LD_LIBRARY_PATH"]),
                             ("libs", str(icd), str(lib)))
            self.assertEqual(env["MESA_LOADER_DRIVER_OVERRIDE"], "zink")
            self.assertEqual(env["VK_ICD_FILENAMES"], str(icd))

    def test_a_view_that_is_not_the_expected_renderer_is_marked_as_a_fallback(self):
        sw = textwrap.dedent("""\
            Surfaceless platform:
            EGL driver name: swrast
            OpenGL ES profile renderer: llvmpipe (LLVM 21.1.8, 128 bits)
            OpenGL ES profile version: OpenGL ES 3.2 Mesa 26.1.8
            OpenGL ES profile extensions:
                GL_EXT_transform_feedback
            """)

        def run(cmd, env=None, cwd=None, timeout=60):
            exe = os.path.basename(cmd[0])
            if exe == "eglinfo":
                return 0, sw, "ZINK: vkCreateInstance failed (VK_ERROR_INCOMPATIBLE_DRIVER)\n"
            if exe == "vulkaninfo":
                return 1, "", "no ICD"
            return 0, '{"context": {"api": "es", "ok": true, "renderer": "llvmpipe (LLVM 21.1.8, 128 bits)", ' \
                      '"version": "OpenGL ES 3.2 Mesa 26.1.8", "extensions": []}}\n{"done": true}\n', "ZINK: failed\n"

        with tempfile.TemporaryDirectory() as t:
            v = gap.collect_view("zink-on-ddk", {}, run, session=None, tmp=t, expect_renderer="zink")
        self.assertIn("llvmpipe", v["fallback"])
        self.assertIn("vkCreateInstance failed", v["stderr"]["eglinfo"])
        # the report never presents a fallback's numbers as the view's
        self.assertEqual(gap.view_versions(v)[0], {"compat": None, "core": None, "es": None})

    def test_missing_vendor_data_is_unknown_not_absent(self):
        # no ES version and no limits recorded for the vendor: nothing is proven either way
        self.assertIsNone(gap.gles_evidence("es:3.0", {}, []))
        self.assertIsNone(gap.gles_evidence("glesint:GL_MAX_TEXTURE_SIZE>=4096", {"es": 32}, [], {}))
        self.assertIs(gap.gles_evidence("glesint:GL_MAX_TEXTURE_SIZE>=4096", {"es": 32}, [], {"GL_MAX_TEXTURE_SIZE": None}), False)
        g = gap.parse_version_gates(VERSION_C)
        needs = {"EXT_transform_feedback": {"needs": ["ext:VK_EXT_transform_feedback"], "class": "project",
                                            "size": "", "source": "s", "ddk_gles": "es:3.0"}}
        r = gap.rank_actions(g, dict(Ranking.FLAGS, ARB_uniform_buffer_object=True), dict(Ranking.CONSTS), vk_view(),
                             vk_view(), {}, [], needs=needs, const_needs={}, apps=[])
        xfb = [i for m in r["milestones"] for i in m["items"] if i["req"] == "ext:VK_EXT_transform_feedback"][0]
        self.assertEqual(xfb["class"], "unknown")

    def test_snapshot_writes_after_every_view_and_isolates_the_gtk_probe(self):
        saved = {k: getattr(gap, k) for k in ("collect_system", "session_env", "collect_view", "collect_gtk4",
                                              "collect_bench", "ddk_vulkan_only_env")}
        seen = {}
        with tempfile.TemporaryDirectory() as t:
            out = Path(t) / "snap.json"

            def view(name, env, *a, **k):
                seen[name] = (dict(env), k.get("override_probe"),
                              sorted(json.loads(out.read_text())["views"]) if out.exists() else None,
                              k.get("expect_renderer"))
                return {"view": name}

            def gtk(env, session, *a, **k):
                raise OSError("fork failed")

            try:
                gap.collect_system = lambda *a, **k: {"stack": "ddk", "temps_c": {}}
                gap.session_env = lambda *a, **k: None
                gap.collect_view = view
                gap.collect_gtk4 = gtk
                gap.ddk_vulkan_only_env = lambda tmp, *a, **k: {"VK_ICD_FILENAMES": "icd", "LD_LIBRARY_PATH": tmp}
                args = gap.main.__globals__["argparse"].Namespace(out=str(out), views=None, bench=False,
                                                                  bench_full=False, reps=1, sizes="640x480")
                with open(os.devnull, "w") as dn:
                    import contextlib
                    with contextlib.redirect_stdout(dn), contextlib.redirect_stderr(dn):
                        gap.snapshot(args)
                        s = json.loads(out.read_text())
                        # the same on the open stack
                        gap.collect_system = lambda *a, **k: {"stack": "open", "temps_c": {}}
                        out.unlink()
                        gap.snapshot(args)
            finally:
                for k, f in saved.items():
                    setattr(gap, k, f)
        self.assertEqual(list(seen), ["ddk", "fedora-mesa", "zink-on-ddk", "open"])
        self.assertEqual(seen["ddk"][0], gap.DDK_ENV)                 # pvr-run's environment
        self.assertIs(seen["ddk"][1], False)                          # no Mesa override probe on the DDK
        self.assertEqual(seen["fedora-mesa"][2], ["ddk"])              # the file holds the first view already
        self.assertEqual(seen["zink-on-ddk"][2], ["ddk", "fedora-mesa"])
        self.assertEqual(s["views"]["ddk"]["view"], "ddk")            # the GTK failure did not replace the view
        self.assertIn("fork failed", s["views"]["ddk"]["gtk4"]["error"])
        # a hardware view that silently lands on llvmpipe must not count as that stack (second review); the
        # expectation is a pattern, so the vendor's exact renderer wording cannot turn the DDK view into a fallback
        pats = {n: e[3] for n, e in seen.items()}
        self.assertIsNone(pats["fedora-mesa"])
        self.assertEqual(pats["zink-on-ddk"], "zink")
        for n in ("ddk", "open"):
            self.assertRegex("PowerVR B-Series BXM-4-64 MC1", pats[n])
            self.assertRegex("zink Vulkan 1.2(PowerVR B-Series BXM-4-64 MC1 (IMAGINATION_OPEN_SOURCE_MESA))", pats[n])
            self.assertNotRegex("llvmpipe (LLVM 21.1.8, 128 bits)", pats[n])

    def test_the_probe_asks_for_the_highest_gles_first(self):
        # a vendor EGL that answers a 2.0 request with exactly 2.0 would reject every ES 3 limit query
        src = Path(SCRIPT).read_text()
        specs = src[src.index("specs = ["):src.index("for api, bind, attempts in specs")]
        es = specs[specs.index('("es"'):specs.index('("compat"')]
        self.assertLess(es.index("[MAJOR, 3, MINOR, 2]"), es.index("[MAJOR, 2]"))

    def test_real_probe_reads_timestamp_bits_only_where_a_timer_query_exists(self):
        r = subprocess.run([str(SCRIPT), "probe"], capture_output=True, text=True, timeout=60)
        p = gap.parse_probe_output(r.stdout, r.returncode, r.stderr)
        ok = [c for c in p["contexts"] if c.get("ok")]
        if not ok:
            self.skipTest("no EGL surfaceless context on this machine")

        def timed(c):
            ext = set(c.get("extensions", []))
            return ("GL_EXT_disjoint_timer_query" in ext if c["api"] == "es"
                    else "GL_ARB_timer_query" in ext or (gap.gl_version_number(c.get("version")) or 0) >= 33)

        for c in ok:
            bits = c["ints"].get("GL_QUERY_COUNTER_BITS_TIMESTAMP")
            if timed(c):
                self.assertTrue(bits and bits[0] > 0, (c["api"], bits))
            else:
                self.assertIsNone(bits, c["api"])

    def test_the_bench_reports_progress_after_every_client_and_size(self):
        progress = []
        out = "    GL_RENDERER:    PowerVR B-Series BXM-4-64\n[build] use-vbo=false: FPS: 100 FrameTime: 1 ms\n"
        r = gap.collect_bench("ddk", {}, {"DISPLAY": ":0"}, run=lambda *a, **k: (0, out, ""), sizes=("640x480", "1000x560"),
                              reps=1, on_progress=lambda res: progress.append(json.dumps(res)))
        self.assertGreaterEqual(len(progress), 4)                      # 2 clients x 2 sizes
        self.assertEqual(r["clients"]["x11"]["640x480"]["median_fps"]["build"], 100)


class ReviewFixes2(unittest.TestCase):
    """the second reviewer pass (2026-09-10): the report must not claim more than the evidence"""

    def test_evidence_and_class_come_from_the_vulkan_item_not_the_gl_flag(self):
        # a flag-level "es:3.2" proved every item of that flag; ES 3.x proves transform feedback, not 1024 compute
        # invocations, and timestamp queries are a feature although their requirement reads like a limit
        g = gap.parse_version_gates(VERSION_C)
        needs = {"EXT_transform_feedback": {
            "needs": ["ext:VK_EXT_transform_feedback", "limit:maxComputeWorkGroupInvocations>=1024",
                      "limit:timestampValidBits>=1"], "class": "project", "size": "", "source": "s", "ddk_gles": "es:3.2"}}
        items = {"ext:VK_EXT_transform_feedback": {"class": "project", "gles": "es:3.0"},
                 "limit:maxComputeWorkGroupInvocations>=1024":
                     {"class": "limit", "gles": "glesint:GL_MAX_COMPUTE_WORK_GROUP_INVOCATIONS>=1024"},
                 "limit:timestampValidBits>=1": {"class": "project", "gles": "glesint:GL_QUERY_COUNTER_BITS_TIMESTAMP>=1"}}
        vvk = vk_view(extensions={"VK_EXT_transform_feedback": 1}, limits={"maxComputeWorkGroupInvocations": 512},
                      queue_families=[{"VkQueueFamilyProperties": {"queueFlags": ["VK_QUEUE_GRAPHICS_BIT"],
                                                                    "timestampValidBits": 64}}])
        r = gap.rank_actions(g, dict(Ranking.FLAGS, ARB_uniform_buffer_object=True), dict(Ranking.CONSTS), vk_view(),
                             vvk, {"es": 32}, [], {"GL_MAX_COMPUTE_WORK_GROUP_INVOCATIONS": [512],
                                                   "GL_QUERY_COUNTER_BITS_TIMESTAMP": [64]},
                             needs=needs, const_needs={}, apps=[], items=items)
        got = {i["req"]: i for m in r["milestones"] for i in m["items"]}
        self.assertEqual(got["limit:maxComputeWorkGroupInvocations>=1024"]["class"], "hardware")   # 512 on both sides
        self.assertEqual(got["limit:timestampValidBits>=1"]["class"], "project")
        self.assertEqual(got["ext:VK_EXT_transform_feedback"]["class"], "project")
        self.assertEqual(got["ext:VK_EXT_transform_feedback"]["vendor_gles"], ["es:3.0"])

    def test_every_requirement_has_an_item_and_the_gles_evidence_is_true(self):
        reqs = {r for k in list(gap.GL_FLAG_NEEDS.values()) + list(gap.CONST_NEEDS.values()) for r in k["needs"]}
        self.assertEqual(sorted(reqs - set(gap.VK_ITEMS)), [])
        for req, it in gap.VK_ITEMS.items():
            self.assertIn(it["class"], ("limit", "project"), req)
            gap.req_eval(req, vk_view())
            if it.get("gles"):
                gap.gles_evidence(it["gles"], {"es": 32}, [], {})
        items = gap.VK_ITEMS
        # no GLES has the BGRA-order packed vertex formats; GLES robustness does not imply image robustness
        self.assertIsNone(items["fmt:VK_FORMAT_A2R10G10B10_UNORM_PACK32:buffer:VK_FORMAT_FEATURE_VERTEX_BUFFER_BIT"]["gles"])
        self.assertEqual(items["fmt:VK_FORMAT_A2B10G10R10_UNORM_PACK32:buffer:VK_FORMAT_FEATURE_VERTEX_BUFFER_BIT"]["gles"],
                         "es:3.0")
        self.assertIsNone(items["feat:robustImageAccess2|feat:robustImageAccess"]["gles"])
        # a timer-query extension may report 0 counter bits: only the bits prove timestamps
        self.assertEqual(items["limit:timestampValidBits>=1"], {"class": "project",
                                                               "gles": "glesint:GL_QUERY_COUNTER_BITS_TIMESTAMP>=1"})
        self.assertEqual(items["limit:maxPerStageDescriptorUniformBuffers>=13"]["class"], "limit")
        self.assertEqual(items["limit:maxComputeWorkGroupInvocations>=1024"]["gles"],
                         "glesint:GL_MAX_COMPUTE_WORK_GROUP_INVOCATIONS>=1024")

    def _snap(self, view, v):
        return {"schema": 1, "stack": view, "system": {"stack": view}, "views": {view: v}}

    def _view(self, renderer, es_version, exts=()):
        eg = {"platforms": [{"name": "Surfaceless", "error": None, "egl": {"driver": "x"},
                             "gl": {"es": {"renderer": renderer, "version": es_version, "extensions": list(exts)}}}]}
        return {"vulkan": vk_view(), "eglinfo": eg, "probe": {}}

    @unittest.skipUnless(GATES.exists(), "gates JSON not generated yet")
    def test_the_report_refuses_a_snapshot_without_the_named_view(self):
        gates = json.loads(GATES.read_text())
        o = self._snap("open", self._view("zink Vulkan 1.2(PowerVR B-Series BXM-4-64 MC1)", "OpenGL ES 2.0 Mesa 26.1.8"))
        lavapipe_only = self._snap("fedora-mesa", self._view("llvmpipe (LLVM 21.1.8, 128 bits)", "OpenGL ES 3.2 Mesa"))
        with self.assertRaises(ValueError):
            gap.render_report(o, lavapipe_only, gates)

    @unittest.skipUnless(GATES.exists(), "gates JSON not generated yet")
    def test_a_fallback_view_is_named_and_none_of_its_numbers_are_used(self):
        gates = json.loads(GATES.read_text())
        o = self._snap("open", self._view("zink Vulkan 1.2(PowerVR B-Series BXM-4-64 MC1)", "OpenGL ES 2.0 Mesa 26.1.8"))
        ddk = self._view("llvmpipe (LLVM 21.1.8, 128 bits)", "OpenGL ES 3.2 Mesa 24.2",
                         ["GL_EXT_geometry_shader", "GL_EXT_tessellation_shader"])
        ddk.update(fallback="no renderer matching /PowerVR|BXM|Imagination/; got: llvmpipe (LLVM 21.1.8, 128 bits)",
                   gtk4={"gtk": "4.22.4", "renderer": "GskGLRenderer"})
        zink = self._view("llvmpipe (LLVM 21.1.8, 128 bits)", "OpenGL ES 3.2 Mesa 26.1.8")
        zink.update(fallback="no renderer matching /zink/; got: llvmpipe (LLVM 21.1.8, 128 bits)",
                    gtk4={"gtk": "4.22.4", "renderer": "GskGLRenderer"})
        vendor = {"schema": 1, "stack": "ddk", "system": {"stack": "ddk"}, "views": {"ddk": ddk, "zink-on-ddk": zink}}
        md = gap.render_report(o, vendor, gates)
        self.assertIn("no renderer matching /zink/", md)
        self.assertIn("no renderer matching /PowerVR|BXM|Imagination/", md)
        self.assertNotIn("GskGLRenderer", md)                  # a fallback's GTK result is not a measurement of it
        self.assertNotIn("DDK GLES proves it", md)             # llvmpipe's ES 3.2 is not vendor evidence

    @unittest.skipUnless(GATES.exists(), "gates JSON not generated yet")
    def test_a_gtk_traceback_is_one_line_in_the_report(self):
        # 2026-09-10: the DDK view's GTK probe failed and its stderr tail (a Python traceback) broke the paragraph
        gates = json.loads(GATES.read_text())
        o = self._snap("open", self._view("zink Vulkan 1.2(PowerVR B-Series BXM-4-64 MC1)", "OpenGL ES 2.0 Mesa 26.1.8"))
        o["views"]["open"]["gtk4"] = {"gtk": "4.22.4", "renderer": "GskVulkanRenderer"}
        ddk = self._view("PowerVR B-Series BXM-4-64", "OpenGL ES 3.2 build 24.2@6603887")
        ddk["gtk4"] = {"error": "rc=1: ^^^^^^^^^^^^^^^^\n  File \"/usr/lib64/python3.14/site-packages/gi/overrides/Gdk.py\", "
                                "line 143, in <module>\n    RGBA = override(RGBA)\n    assert g_type != TYPE_NONE\n"
                                "           ^^^^^^^^^^^^^^^^^^^\nAssertionError"}
        md = gap.render_report(o, self._snap("ddk", ddk), gates)
        line = next(l for l in md.splitlines() if l.startswith("Measured on this board, a real GTK window"))
        self.assertIn("open: GTK 4.22.4 GskVulkanRenderer", line)
        self.assertTrue(line.endswith("ddk: GTK probe failed: rc=1: AssertionError."), line)   # ddk = last view
        self.assertNotIn("File \"", md)
        self.assertNotIn("^^^", md)

    def test_gtk_error_summary_keeps_one_meaningful_line(self):
        s = gap.gtk_error_summary
        tb = 'rc=1: ^^^^\n  File "x.py", line 1, in <module>\n    assert g_type != TYPE_NONE\nAssertionError'
        self.assertEqual(s(tb), "rc=1: AssertionError")
        # run_cmd appends the command (the whole probe script) on a timeout: the tail is script text, not the error
        self.assertEqual(s("rc=124: Gtk-WARNING\ntimeout after 40s: python3 -c import gi\nprint(json.dumps(res))"),
                         "rc=124: timed out")
        self.assertEqual(s("rc=125: unkillable after the 40s timeout (D state?): python3 -c import gi"),
                         "rc=125: did not exit after the kill (D state?)")
        # snapshot() records "<ExceptionType>: <message>"; a message over several lines keeps its type
        self.assertEqual(s("RuntimeError: first line\nsecond line"), "RuntimeError: first line")
        self.assertEqual(s("rc=1: ImportError: No module named gi"), "rc=1: ImportError: No module named gi")
        nosess = "no desktop session (the GTK probe opens a window on the logged-in desktop)"
        self.assertEqual(s(nosess), nosess)
        self.assertEqual(s("  \n "), "no error text")

    def test_a_gtk_probe_timeout_does_not_record_the_script(self):
        sess = {"WAYLAND_DISPLAY": "wayland-0"}
        tail = "Gtk-WARNING: x\ntimeout after 40s: /usr/bin/python3 -c import gi\nprint(json.dumps(res))"
        self.assertEqual(gap.collect_gtk4({}, sess, lambda *a, **k: (124, "", tail)),
                         {"error": "rc=124: timed out after 40 s"})
        self.assertEqual(gap.collect_gtk4({}, sess, lambda *a, **k: (125, "", "unkillable ...: python3 -c import gi")),
                         {"error": "rc=125: did not exit after the kill (D state?)"})


class BuildScript(unittest.TestCase):
    """tools/mesa/build-pvr-vulkan.sh: the two pipefail traps found on 2026-09-10"""
    SRC = (P / "tools" / "mesa" / "build-pvr-vulkan.sh").read_text()

    def test_no_early_exit_pipe_can_trip_pipefail(self):
        # `x | grep -q` returns 141 under pipefail when grep stops early, i.e. exactly when it matched
        self.assertIn("set -euo pipefail", self.SRC)
        code = "\n".join(line for line in self.SRC.splitlines() if not line.lstrip().startswith("#"))
        self.assertNotRegex(code, r"\|\s*grep\s+-q")

    def test_install_check_proves_which_driver_the_loader_picked(self):
        # Fedora's and the a7a driver both say "Mesa 26.1.8": only the loader's own log tells them apart
        inst = self.SRC[self.SRC.index('if [ "$INSTALL" = 1 ]'):]
        self.assertIn("VK_LOADER_DEBUG=driver", inst)
        self.assertIn("/usr/local/lib64/libvulkan_powervr_mesa.so", inst)


if __name__ == "__main__":
    unittest.main()
