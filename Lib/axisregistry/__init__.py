from copy import deepcopy
from fontTools.ttLib import TTFont
from fontTools.otlLib.builder import buildStatTable
from fontTools.varLib.instancer.names import _updateUniqueIdNameRecord, NameID
from fontTools.ttLib.tables._f_v_a_r import NamedInstance
from pkg_resources import resource_filename
from google.protobuf import text_format
from collections import OrderedDict
from axisregistry.axes_pb2 import AxisProto
from collections import defaultdict
from itertools import chain
import logging
from glob import glob
import os

try:
    from ._version import version as __version__  # type: ignore
except ImportError:
    __version__ = "0.0.0+unknown"

log = logging.getLogger(__file__)

# TODO we may have more of these. Please note that some applications may not
# implement variable font style linking.
LINKED_VALUES = {
    "wght": {400.0: 700.0},
    "ital": {0.0: 1.0},
}

# Static font styles. The GF api only support the following static font styles
GF_STATIC_STYLES = OrderedDict(
    [
        ("Thin", 100),
        ("ExtraLight", 200),
        ("Light", 300),
        ("Regular", 400),
        ("Medium", 500),
        ("SemiBold", 600),
        ("Bold", 700),
        ("ExtraBold", 800),
        ("Black", 900),
        ("Thin Italic", 100),
        ("ExtraLight Italic", 200),
        ("Light Italic", 300),
        ("Italic", 400),
        ("Medium Italic", 500),
        ("SemiBold Italic", 600),
        ("Bold Italic", 700),
        ("ExtraBold Italic", 800),
        ("Black Italic", 900),
    ]
)


def load_protobuf(klass, path):
    message = klass()
    with open(path, "rb") as text_data:
        text_format.Merge(text_data.read(), message)
    return message


class AxisRegistry:
    def __init__(self, fp=resource_filename("axisregistry", "data")):
        axis_fps = [fp for fp in glob(os.path.join(fp, "*.textproto"))]
        self._data = {}
        for fp in axis_fps:
            axis = load_protobuf(AxisProto, fp)
            self._data[axis.tag] = axis

    def __getitem__(self, k):
        return self._data[k]

    def __iter__(self):
        for i in self._data:
            yield i

    def get_fallback(self, name):
        for a in self:
            for fallback in self[a].fallback:
                if name == fallback.name:
                    return a, fallback
        return None, None

    def fallbacks_in_fvar(self, ttFont):
        res = defaultdict(list)
        axes_in_font = {
            a.axisTag: {"min": a.minValue, "max": a.maxValue}
            for a in ttFont["fvar"].axes
        }
        for axis in self:
            if axis not in axes_in_font:
                log.warn(f"Axis {axis} not found in GF Axis Registry!")
                continue
            for fallback in self[axis].fallback:
                if (
                    fallback.value < axes_in_font[axis]["min"]
                    or fallback.value > axes_in_font[axis]["max"]
                ):
                    continue
                res[axis].append(fallback)
        return res

    def fallbacks_in_name_table(self, ttFont):
        res = []
        name_table = ttFont["name"]
        tokens = (
            name_table.getBestFamilyName().split()
            + name_table.getBestSubFamilyName().split()
        )
        fvar_axes_in_font = [a.axisTag for a in ttFont["fvar"].axes]
        for token in tokens:
            axis, fallback = axis_registry.get_fallback(token)
            if not axis or axis in fvar_axes_in_font:
                continue
            res.append((axis, fallback))
        return res


axis_registry = AxisRegistry()


def is_variable(ttFont):
    return "fvar" in ttFont


def _fvar_dflts(ttFont):
    res = OrderedDict()
    for a in ttFont["fvar"].axes:
        # find name and elision
        if a.axisTag in axis_registry:
            name = next(
                (
                    f.name
                    for f in axis_registry[a.axisTag].fallback
                    if f.value == a.defaultValue
                ),
                None,
            )
            elided = a.defaultValue == axis_registry[
                a.axisTag
            ].default_value and name not in ["Regular", "Italic"]
        else:
            name = None
            elided = True  # since we can't find a name for it, keep it elided

        res[a.axisTag] = {"value": a.defaultValue, "name": name, "elided": elided}
    return res


def build_stat(ttFont, sibling_ttFonts=None):
    log.info("Building STAT table")
    assert is_variable(ttFont), "not a VF!"
    fallbacks_in_fvar = axis_registry.fallbacks_in_fvar(ttFont)
    fallbacks_in_siblings = list(
        chain.from_iterable(
            axis_registry.fallbacks_in_name_table(f) for f in sibling_ttFonts
        )
    )
    fallbacks_in_names = axis_registry.fallbacks_in_name_table(ttFont)
    nametable = ttFont["name"]

    # rm old STAT table and associated name table records
    if "STAT" in ttFont:
        stat = ttFont["STAT"]
        axis_values = stat.table.AxisValueArray.AxisValue
        axes = stat.table.DesignAxisRecord.Axis
        for ax in axis_values:
            nametable.removeNames(nameID=ax.ValueNameID)
        for ax in axes:
            nametable.removeNames(nameID=ax.AxisNameID)
        del ttFont["STAT"]

    res = []
    # use fontTools build_stat
    # https://github.com/fonttools/fonttools/blob/a293606fc8c88af8510d0688a6a36271ff4ff350/Lib/fontTools/otlLib/builder.py#L2683
    seen_axes = set()
    for axis, fallbacks in fallbacks_in_fvar.items():
        seen_axes.add(axis)
        a = {"tag": axis, "name": axis_registry[axis].display_name, "values": []}
        for fallback in fallbacks:
            a["values"].append(
                {
                    "name": fallback.name,
                    "value": fallback.value,
                    # include flags and linked values
                    "flags": 0x2
                    if fallback.value == axis_registry[axis].default_value
                    else 0x0,
                }
            )
            if axis in LINKED_VALUES and fallback.value in LINKED_VALUES[axis]:
                a["values"][-1]["linkedValue"] = LINKED_VALUES[axis][fallback.value]
        res.append(a)

    if fallbacks_in_names:
        for axis, fallback in fallbacks_in_names:
            if axis in seen_axes:
                continue
            a = {
                "tag": axis,
                "name": axis_registry[axis].display_name,
                "values": [
                    {"name": fallback.name, "value": fallback.value, "flags": 0x0}
                ],
            }
            if axis in LINKED_VALUES and fallback.value in LINKED_VALUES[axis]:
                a["values"][0]["linkedValue"] = LINKED_VALUES[axis][fallback.value]
            res.append(a)

    if fallbacks_in_siblings:
        for axis, fallback in fallbacks_in_siblings:
            if axis in seen_axes:
                continue
            value = 0.0
            a = {
                "tag": axis,
                "name": axis_registry[axis].display_name,
                "values": [{"name": "Normal", "value": value, "flags": 0x2}],
            }
            if axis in LINKED_VALUES and value in LINKED_VALUES[axis]:
                a["values"][0]["linkedValue"] = LINKED_VALUES[axis][value]
            res.append(a)
    buildStatTable(ttFont, res, macNames=False)


def build_name_table(ttFont, family_name=None, style_name=None, siblings=[]):
    log.info("Building name table")
    name_table = ttFont["name"]
    family_name = family_name if family_name else name_table.getBestFamilyName()
    style_name = style_name if style_name else name_table.getBestSubFamilyName()
    if is_variable(ttFont):
        return build_vf_name_table(ttFont, family_name, siblings=siblings)
    return build_static_name_table_v1(ttFont, family_name, style_name)


def build_vf_name_table(ttFont, family_name, siblings=[]):
    # VF name table should reflect the 0 origin of the font!
    assert is_variable(ttFont), "Not a VF!"
    style_name = _vf_style_name(ttFont)
    # if there are sibling fonts and the style name isn't wght+ital, use the v1 static method
    if siblings and style_name not in GF_STATIC_STYLES:
        build_static_name_table_v1(ttFont, family_name, style_name)
    else:
        build_static_name_table(ttFont, family_name, style_name)

    # set nameID25.
    font_styles = axis_registry.fallbacks_in_name_table(ttFont)
    if font_styles:
        vf_ps = family_name.replace(" ", "") + "".join(
            [
                fallback.name
                for _, fallback in font_styles
                if fallback.name not in family_name
            ]
        )
    else:
        vf_ps = family_name.replace(" ", "")
    ttFont["name"].setName(vf_ps, NameID.VARIATIONS_POSTSCRIPT_NAME_PREFIX, 3, 1, 0x409)


def _vf_style_name(ttFont):
    fvar_dflts = _fvar_dflts(ttFont)
    res = []
    for k, v in fvar_dflts.items():
        if v["elided"]:
            continue
        res.append(v["name"])

    font_styles = axis_registry.fallbacks_in_name_table(ttFont)
    for _, s in font_styles:
        if s.name in res:
            continue
        res.append(s.name)
    name = " ".join(res).replace("Regular Italic", "Italic")
    log.debug(f"Built VF style name: '{name}'")
    return name


def build_fvar_instances(ttFont, axis_dflts={}):
    """Replace a variable font's fvar instances with a set of new instances
    which conform to the Google Fonts instance spec:
    https://github.com/googlefonts/gf-docs/tree/main/Spec#fvar-instances
    """
    assert is_variable(ttFont), "Not a VF!"
    log.info("Building fvar instances")
    fvar = ttFont["fvar"]
    name_table = ttFont["name"]
    style_name = name_table.getBestSubFamilyName()

    # rm old fvar subfamily and ps name records
    for inst in fvar.instances:
        name_table.removeNames(nameID=inst.subfamilyNameID)
        if inst.postscriptNameID != 65535:
            name_table.removeNames(nameID=inst.postscriptNameID)

    fvar_dflts = _fvar_dflts(ttFont)
    if not axis_dflts:
        axis_dflts = {k: v["value"] for k, v in fvar_dflts.items()}

    is_italic = "Italic" in style_name
    is_roman_and_italic = any(a for a in ("slnt", "ital") if a in fvar_dflts)

    if "wght" not in fvar_dflts:
        # TODO
        raise NotImplementedError()

    ital_axis = next((a for a in fvar.axes if a.axisTag == "ital"), None)
    slnt_axis = next((a for a in fvar.axes if a.axisTag == "slnt"), None)

    fallbacks = axis_registry.fallbacks_in_fvar(ttFont)
    wght_fallbacks = fallbacks["wght"]

    def gen_instances(is_italic):
        results = []
        for fallback in wght_fallbacks:
            name = fallback.name if not is_italic else f"{fallback.name} Italic".strip()
            name = name.replace("Regular Italic", "Italic")

            coordinates = {k: v for k, v in axis_dflts.items()}
            coordinates["wght"] = fallback.value
            if is_italic:
                if ital_axis:
                    coordinates["ital"] = ital_axis.minValue
                elif slnt_axis:
                    coordinates["slnt"] = slnt_axis.minValue

            inst = NamedInstance()
            inst.subfamilyNameID = name_table.addName(name)
            inst.coordinates = coordinates
            log.debug(f"Adding fvar instance: {name}: {coordinates}")
            results.append(inst)
        return results

    instances = []
    if is_roman_and_italic:
        for bool_ in (False, True):
            instances += gen_instances(is_italic=bool_)
    elif is_italic:
        instances += gen_instances(is_italic=True)
    else:
        instances += gen_instances(is_italic=False)
    fvar.instances = instances


def build_static_name_table(ttFont, family_name, style_name):
    # stip mac names
    name_table = ttFont["name"]
    name_table.removeNames(platformID=1)
    existing_name = ttFont["name"].getBestFamilyName()

    names = {}
    is_ribbi = (
        True if style_name in ("Regular", "Italic", "Bold", "Bold Italic") else False
    )
    if is_ribbi:
        full_name = f"{family_name} {style_name}"
        ps_name = f"{family_name}-{style_name}".replace(" ", "")
        names[(NameID.FAMILY_NAME, 3, 1, 0x409)] = family_name
        names[(NameID.SUBFAMILY_NAME, 3, 1, 0x409)] = style_name
        names[(NameID.FULL_FONT_NAME, 3, 1, 0x409)] = full_name
        names[(NameID.POSTSCRIPT_NAME, 3, 1, 0x409)] = ps_name
        for name_id in (
            NameID.TYPOGRAPHIC_FAMILY_NAME,
            NameID.TYPOGRAPHIC_SUBFAMILY_NAME,
            21,
            22,
        ):
            name_table.removeNames(nameID=name_id)
    else:
        style_tokens = style_name.split()
        new_family_name = family_name.split()
        is_italic = "Italic" in style_tokens
        for t in style_tokens:
            if t in ["Regular", "Italic"] or t in new_family_name:
                continue
            new_family_name.append(t)
        new_family_name = " ".join(new_family_name)
        new_style_name = "Italic" if is_italic else "Regular"
        full_name = f"{family_name} {style_name}"
        ps_name = f"{family_name}-{style_name}".replace(" ", "")

        names[(NameID.FAMILY_NAME, 3, 1, 0x409)] = new_family_name
        names[(NameID.SUBFAMILY_NAME, 3, 1, 0x409)] = new_style_name
        names[(NameID.FULL_FONT_NAME, 3, 1, 0x409)] = full_name
        names[(NameID.POSTSCRIPT_NAME, 3, 1, 0x409)] = ps_name
        names[(NameID.TYPOGRAPHIC_FAMILY_NAME, 3, 1, 0x409)] = family_name
        names[(NameID.TYPOGRAPHIC_SUBFAMILY_NAME, 3, 1, 0x409)] = style_name
        # we do not use WWS names since we use the RIBBI naming schema
        for name_id in (21, 22):
            name_table.removeNames(nameID=name_id)

    names[(NameID.UNIQUE_FONT_IDENTIFIER, 3, 1, 0x409)] = _updateUniqueIdNameRecord(
        ttFont, {k[0]: v for k, v in names.items()}, (3, 1, 0x409)
    )
    for k, v in names.items():
        log.debug(f"Adding name record {k}: {v}")
        name_table.setName(v, *k)

    # Replace occurences of old family name in untouched records
    skip_ids = [i.numerator for i in NameID]
    for r in ttFont["name"].names:
        if r.nameID in skip_ids:
            continue
        current = r.toUnicode()
        if existing_name not in current:
            continue
        if " " not in current:
            replacement = current.replace(existing_name, family_name).replace(" ", "")
        else:
            replacement = current.replace(existing_name, family_name)
        ttFont["name"].setName(
            replacement, r.nameID, r.platformID, r.platEncID, r.langID
        )


def build_static_name_table_v1(ttFont, family_name, style_name):
    """Pre VF name tables, this version can only accept wght + ital"""
    non_weight_tokens = []
    v1_tokens = []
    tokens = style_name.split()
    for t in tokens:
        if t not in GF_STATIC_STYLES:
            non_weight_tokens.append(t)
        else:
            v1_tokens.append(t)

    family_tokens = family_name.split()
    new_family_name = []
    for t in family_tokens:
        if t in non_weight_tokens or t in new_family_name:
            continue
        new_family_name.append(t)
    for t in non_weight_tokens:
        new_family_name.append(t)

    family_name = " ".join(new_family_name)
    style_name = " ".join(v1_tokens).replace("Regular Italic", "Italic").strip()
    style_name = style_name or "Regular"
    log.debug(f"New family name: {family_name}")
    log.debug(f"New style name: {style_name}")
    build_static_name_table(ttFont, family_name, style_name)


def build_filename(ttFont):
    name_table = ttFont["name"]
    family_name = name_table.getBestFamilyName()
    style_name = name_table.getBestSubFamilyName()
    _, ext = os.path.splitext(ttFont.reader.file.name)
    if is_variable(ttFont):
        is_italic = "Italic" in style_name
        axes = _fvar_dflts(ttFont).keys()
        axes = sorted([a for a in axes if a.isupper()]) + sorted(
            [a for a in axes if a.islower()]
        )
        if is_italic:
            return f"{family_name}-Italic[{','.join(axes)}]{ext}".replace(" ", "")
        return f"{family_name}[{','.join(axes)}]{ext}".replace(" ", "")
    return f"{family_name}-{style_name}{ext}".replace(" ", "")
