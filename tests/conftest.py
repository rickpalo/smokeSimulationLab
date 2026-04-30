"""
Minimal bpy stub so the addon's pure-Python functions can be imported
and tested without a running Blender instance.

Only the subset used by the functions under test is stubbed.
"""
import sys
import types


def _make_bpy_stub():
    bpy = types.ModuleType("bpy")

    # bpy.props — property constructors return a (type, kwargs) tuple so the
    # PropertyGroup annotation syntax works without Blender's metaclass.
    props = types.ModuleType("bpy.props")
    for _pname in (
        "FloatProperty", "IntProperty", "BoolProperty",
        "StringProperty", "EnumProperty", "PointerProperty",
        "CollectionProperty",
    ):
        setattr(props, _pname, lambda *a, _n=_pname, **kw: (_n, kw))
    bpy.props = props

    # bpy.types — only the names referenced at module level are needed.
    btypes = types.ModuleType("bpy.types")
    btypes.PropertyGroup   = object
    btypes.UIList          = object
    btypes.Operator        = object
    btypes.Panel           = object
    btypes.AddonPreferences = object
    btypes.Object          = object
    btypes.Scene           = object
    bpy.types = btypes

    # bpy.app — only handlers / timers stubs needed.
    app = types.ModuleType("bpy.app")
    handlers = types.SimpleNamespace(
        load_post=[],
        persistent=lambda f: f,   # decorator no-op
    )
    timers = types.SimpleNamespace(
        register=lambda *a, **kw: None,
        unregister=lambda *a, **kw: None,
        is_registered=lambda *a: False,
    )
    app.handlers = handlers
    app.timers   = timers
    app.binary_path = ""
    bpy.app = app

    # bpy.data / bpy.context / bpy.path — minimal stubs.
    bpy.data    = types.SimpleNamespace(filepath="", scenes=[], is_dirty=False)
    bpy.context = types.SimpleNamespace()
    bpy.path    = types.SimpleNamespace(abspath=lambda p: p)

    bpy.utils = types.SimpleNamespace(
        register_class=lambda c: None,
        unregister_class=lambda c: None,
    )
    return bpy


# Install the stub BEFORE any addon code is imported.
sys.modules["bpy"] = _make_bpy_stub()
