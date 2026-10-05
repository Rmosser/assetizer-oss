from fontTools.ttLib import TTFont
from fontTools.varLib import instancer
from fontTools import subset
import os
src = TTFont("NotoSerifSC-VF.ttf")
feats = sorted({fr.FeatureTag for fr in src["GPOS"].table.FeatureList.FeatureRecord}) if "GPOS" in src else []
print("GPOS features:", feats)
print("GSUB features:", sorted({fr.FeatureTag for fr in src["GSUB"].table.FeatureList.FeatureRecord}))
out = "out"; os.makedirs(out, exist_ok=True)
PUNCT = "U+3000-3003,U+3008-3011,U+3014-301F,U+FF01-FF0F,U+FF1A-FF20,U+FF3B-FF40,U+FF5B-FF65,U+2014,U+2018-2019,U+201C-201D,U+2026"
WEEKDAY = "〇一二三四五六七八九十月日年星期频道机房刊审定"
def build(w, unicodes=None, text=None, name=""):
    f = instancer.instantiateVariableFont(TTFont("NotoSerifSC-VF.ttf"), {"wght": w}, updateFontNames=False)
    opts = subset.Options()
    opts.flavor = "woff2"
    opts.layout_features = ["halt", "vhal", "chws", "vchw", "palt", "kern", "vert", "locl"]
    opts.name_IDs = ["*"]; opts.notdef_outline = True; opts.hinting = False
    s = subset.Subsetter(opts)
    if unicodes: s.populate(unicodes=subset.parse_unicodes(unicodes))
    if text: s.populate(text=text)
    s.subset(f)
    p = f"{out}/{name}-{w}.woff2"
    f.flavor = "woff2"; f.save(p)
    print(p, os.path.getsize(p))
for w in (400, 700):
    build(w, unicodes=PUNCT, name="az-punct-serif")
build(900, text=WEEKDAY, name="az-display")
