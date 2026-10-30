"""
Чертежи (.cdw) по готовым деталям (.m3d) — автоматически, с размерами.

  py make_drawings.py out_module                  # габаритные чертежи (ГЧ) по ГОСТ 2.109-73
  py make_drawings.py out_module --author Иванов   # фамилия в основную надпись
  py make_drawings.py out_module --no-llm          # размеры выбирает программа, без модели
  py make_drawings.py out_module --type full       # прежний режим: все размеры для изготовления

Габаритный чертёж (по умолчанию): только габаритные, установочные, присоединительные и эксплуатационные
размеры. Программа находит все возможные размеры и описывает их назначение, модель по правилам
ГОСТ 2.102 / 2.109-73 (prompts/drawing.md) выбирает нужные; установочные и присоединительные — с отклонениями ±0,2.

Для каждой детали (нужен .json рядом с .m3d — его пишет run.py):
  - формат A4/A3 и стандартный масштаб по габариту;
  - виды спереди, сверху, слева (+ изометрия, если есть место);
  - размеры: габаритные, диаметры отверстий с количеством («4 отв. Ø3,25»),
    координаты центров отверстий от кромок, размеры карманов и пазов, диаметры ступеней;
  - основная надпись и технические требования.
Проверьте чертёж глазами: размеры могут налезать друг на друга — подвиньте мышью
(это тоже ручная правка — запишите в журнал).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import traceback
from pathlib import Path

import pythoncom

from agent.drawdims import (CATS, candidates, dim_line_pos, heuristic_keep, layout, mandatory_holes, pick_format, plan,
                            plan_selected)
from agent.dsl import normalize

DENSITY = 1.24e-3  # PLA, г/мм³
PROJ = {"front": "#Спереди", "top": "#Сверху", "left": "#Слева", "iso": "#Изометрия XYZ"}
TT_GCH = [
    "1. Чертеж габаритный. Указаны габаритные, установочные, присоединительные и эксплуатационные размеры по ГОСТ 2.109-73.",
    "2. Установочные и присоединительные размеры указаны с предельными отклонениями.",
]
TT = [
    "1. Изготовить методом FDM-печати.",
    "2. Материал PLA, высота слоя 0,2 мм, заполнение 30 %.",
    "3. Печать без поддержек.",
    "4. Неуказанные предельные отклонения размеров ±0,2 мм.",
]


def scale_str(s: float) -> str:
    return (f"{s:g}:1" if s >= 1 else f"1:{1 / s:g}").replace(".", ",")


def qi(api7, obj, cls_name):
    cls = getattr(api7, cls_name)
    try:
        return cls(obj._oleobj_.QueryInterface(cls.CLSID, pythoncom.IID_IDispatch))
    except Exception:
        return cls(obj)


class Drawer:
    def __init__(self):
        from win32com.client import Dispatch, gencache
        self.c5 = gencache.EnsureModule("{75C9F5D0-B5B8-4526-8681-9903C567D2ED}", 0, 1, 0).constants
        self.api7 = gencache.EnsureModule("{69AC2981-37C0-4379-84FD-5DD2F3C0A520}", 0, 1, 0)
        self.app = self.api7.IApplication(Dispatch("Kompas.Application.7")._oleobj_.QueryInterface(
            self.api7.IApplication.CLSID, pythoncom.IID_IDispatch))
        self.app.Visible = True
        self.fails = 0

    def step(self, name, fn):
        try:
            return fn()
        except Exception as e:
            self.fails += 1
            print(f"   ! «{name}»: {type(e).__name__}: {e}")
            return None

    # ---------------------------------------------------------------- размеры
    def _prefix(self, d, text):
        if not text:
            return
        try:
            qi(self.api7, d, "IDimensionText").Prefix.Str = text
        except Exception as e:
            print(f"   ! префикс «{text}»: {e}")

    def _suffix(self, d, text):
        if not text:
            return
        try:
            qi(self.api7, d, "IDimensionText").Suffix.Str = text
        except Exception as e:
            print(f"   ! отклонение «{text}»: {e}")

    def lin_dim(self, sc, d, box, s):
        (x3, y3), ori = dim_line_pos(d, box, s)
        dim = sc.LineDimensions.Add()
        (x1, y1), (x2, y2) = d["p1"], d["p2"]
        dim.X1, dim.Y1, dim.X2, dim.Y2 = float(x1), float(y1), float(x2), float(y2)
        dim.X3, dim.Y3 = float(x3), float(y3)
        try:
            dim.Orientation = (getattr(self.c5, "ksLinDHorizontal", 0) if ori == "h"
                               else getattr(self.c5, "ksLinDVertical", 1))
        except Exception:
            pass
        if d.get("diameter"):
            # знак диаметра — свойством Sign (буква «Ø» в шрифте ГОСТ рисуется как «Ш»)
            try:
                dt = qi(self.api7, dim, "IDimensionText")
                dt.Sign = getattr(getattr(self.api7, "constants", None), "ksDimSDiameter", None) or \
                    getattr(self.c5, "ksDimSDiameter", None) or 1
            except Exception as e:
                print(f"   ! знак диаметра: {e}")
        self._suffix(dim, d.get("suffix"))
        dim.Update()

    def diam_dim(self, sc, d):
        dim = sc.DiametralDimensions.Add()
        dim.Xc, dim.Yc, dim.Radius = float(d["c"][0]), float(d["c"][1]), float(d["r"])
        dim.Angle = float(d.get("angle", 45.0))
        self._prefix(dim, d.get("prefix", ""))
        self._suffix(dim, d.get("suffix"))
        dim.Update()

    # ---------------------------------------------------------------- чертёж
    def make(self, m3d: Path, info: dict, author: str, P: dict | None = None, gch: bool = False) -> Path:
        api7, c5 = self.api7, self.c5
        prims = normalize(info["spec"])
        P = P or plan(prims)
        fmt_name, s = pick_format(P["lo"], P["hi"])
        origins, right_edge = layout(P["lo"], P["hi"], s, fmt_name)
        print(f"   формат {fmt_name}, масштаб {scale_str(s)}")

        doc = self.app.Documents.Add(getattr(c5, "ksDocumentDrawing", 1), True)
        doc2d = qi(api7, doc, "IKompasDocument2D")

        def set_format():
            sh = doc.LayoutSheets.Item(0)
            sh.Format.Format = getattr(c5, "ksFormatA4" if fmt_name == "A4" else "ksFormatA3", 4 if fmt_name == "A4" else 3)
            sh.Format.VerticalOrientation = fmt_name == "A4"
            sh.Update()
        self.step("формат листа", set_format)

        views = doc2d.ViewsAndLayersManager.Views
        made = {}
        wanted = P.get("views") or list(origins)
        for key, (x, y) in origins.items():
            if key not in wanted:  # вид без размеров не строим
                continue
            def add_view(key=key, x=x, y=y):
                v = views.Add(getattr(c5, "vt_Arbitrary", 2))
                av = qi(api7, v, "IAssociationView")
                av.SourceFileName = str(m3d.resolve())
                av.ProjectionName = PROJ[key]
                av.Scale = s
                av.X, av.Y = float(x), float(y)
                v.Update()
                return v
            v = self.step(f"вид {PROJ[key]}", add_view)
            if v is not None:
                made[key] = v
        n_dims = 0
        for key, v in made.items():
            sc = self.step(f"размеры: контейнер вида {key}", lambda v=v: qi(api7, v, "ISymbols2DContainer"))
            if sc is None:
                continue
            box = P["boxes"][key]
            for d in P["dims"].get(key, []):
                fn = (lambda d=d: self.lin_dim(sc, d, box, s)) if d["kind"] == "lin" else (lambda d=d: self.diam_dim(sc, d))
                before = self.fails
                self.step(f"размер на виде {key}", fn)
                n_dims += self.fails == before
        print(f"   размеров поставлено: {n_dims}")

        def stamp():
            st = doc.LayoutSheets.Item(0).Stamp
            vals = {1: info["name"] + ("\nГабаритный чертёж" if gch else ""), 2: info["code"] + (" ГЧ" if gch else ""),
                    3: "PLA", 5: f"{info['mass']:.1f}".replace(".", ","),
                    6: scale_str(s), 110: author, 9: "Команда 6"}
            for cell, text in vals.items():
                try:
                    st.Text(cell).Str = text
                except Exception as e:
                    print(f"   ! графа {cell}: {e}")
            st.Update()
        self.step("основная надпись", stamp)

        def tech():
            td = qi(api7, doc, "IDrawingDocument").TechnicalDemand
            t = td.Text
            for line in (TT_GCH if gch else TT):
                t.Add().Str = line
            td.Update()
        self.step("технические требования", tech)

        out = m3d.with_suffix(".cdw")
        ok = self.step("сохранение", lambda: doc.SaveAs(str(out.resolve())))
        if ok is False:
            out = out.with_name(out.stem + "_new.cdw")
            self.step("сохранение (запасное имя)", lambda: doc.SaveAs(str(out.resolve())))
        return out


def choose_dims(C: dict, text: str, name: str, llm) -> tuple[dict, str]:
    """Выбор размеров для габаритного чертежа: модель по ГОСТ (prompts/drawing.md), иначе — правила программы."""
    if llm is None:
        return heuristic_keep(C["items"]), "выбор программой (без модели)"
    from agent.solver import PROMPT_DIR, extract_json
    lst = "\n".join(f"{it['id']}: {it['desc']} (вид: {it['view']}; подсказка: {it['hint']})" for it in C["items"])
    user = (f"Задание на деталь:\n{text.strip()}\n\nКандидаты размеров:\n{lst}\n\n"
            "Выбери размеры для габаритного чертежа. Верни только JSON.")
    msgs = [{"role": "system", "content": (PROMPT_DIR / "drawing.md").read_text(encoding="utf-8")},
            {"role": "user", "content": user}]
    try:
        data = extract_json(llm.chat(msgs, 0.0, f"{name}:чертёж"))
        ids = {it["id"]: it for it in C["items"]}
        keep = {}
        for k in data.get("keep", []):
            i, cat = str(k.get("id")), str(k.get("cat", "")).lower()
            if i in ids:
                keep[i] = cat if cat in CATS else ids[i]["hint"]
        for i, it in ids.items():  # габариты — всегда
            if i.startswith("G"):
                keep[i] = "габаритный"
        for i, cat in mandatory_holes(C["items"]).items():  # все отверстия с положением — всегда
            keep.setdefault(i, cat)
        return keep, "модель: " + str(data.get("note", ""))[:200]
    except Exception as e:
        return heuristic_keep(C["items"]), f"модель недоступна ({type(e).__name__}: {e}) — выбор программой"


def make_llm():
    import yaml
    from dotenv import load_dotenv

    from agent.llm import LLMClient, RequestLog
    from agent.privacy import Privacy
    root = Path(__file__).resolve().parent
    load_dotenv(root / ".env")
    cfg = yaml.safe_load((root / "config.yaml").read_text(encoding="utf-8"))
    pv = cfg.get("privacy", {})
    return LLMClient(cfg, RequestLog(root / cfg.get("log_file", "logs/requests.jsonl")),
                     Privacy.from_config(pv, root))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("folder", help="папка с .m3d и .json от run.py")
    ap.add_argument("--author", default="ИИ-агент")
    ap.add_argument("--prefix", default="МТМ", help="префикс обозначения")
    ap.add_argument("--type", choices=("gch", "full"), default="gch",
                    help="gch — габаритный чертёж по ГОСТ 2.109-73 (по умолчанию), full — все размеры")
    ap.add_argument("--no-llm", action="store_true", help="размеры выбирает программа, без модели")
    a = ap.parse_args()
    llm = None
    if a.type == "gch" and not a.no_llm:
        try:
            llm = make_llm()
        except Exception as e:
            print(f"модель недоступна ({e}) — размеры выберет программа")
    folder = Path(a.folder)
    parts = sorted(p for p in folder.glob("*.m3d") if not re.search(r"_\d{6}$", p.stem))
    if not parts:
        sys.exit(f"в {folder} нет .m3d")
    d = Drawer()
    for i, m3d in enumerate(parts, 1):
        js = m3d.with_suffix(".json")
        if not js.exists():
            print(f"{m3d.name}: нет {js.name} — пропускаю (нужен JSON от run.py)")
            continue
        r = json.loads(js.read_text(encoding="utf-8"))
        if not r.get("spec"):
            print(f"{m3d.name}: в JSON нет описания детали — пропускаю")
            continue
        src = r.get("kompas") or r.get("ref") or {}
        vol = src.get("volume") or (r.get("ref") or {}).get("volume") or 0
        words = re.findall(r"[А-ЯЁа-яё\-]+", r.get("text", ""))
        info = {"name": words[0].capitalize() if words else m3d.stem, "code": f"{a.prefix}.{i:03d}",
                "mass": vol * DENSITY, "spec": r["spec"]}
        print(f"{m3d.name}: {info['name']}, масса {info['mass']:.1f} г")
        P = None
        if a.type == "gch":
            C = candidates(normalize(r["spec"]))
            keep, how = choose_dims(C, r.get("text", ""), m3d.stem, llm)
            P = plan_selected(C, keep)
            print(f"   размеры ({how}):")
            for it in C["items"]:
                if it["id"] in keep:
                    print(f"     {keep[it['id']]:17s} {it['desc']}")
        try:
            out = d.make(m3d, info, a.author, P, gch=a.type == "gch")
            print(f"   → {out.name}")
        except Exception:
            traceback.print_exc(limit=3)
    print("\nготово. Откройте чертежи, подвиньте налезающие размеры, запишите правки в журнал.")


if __name__ == "__main__":
    main()
