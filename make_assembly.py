"""
Сборка (.a3d) из готовых деталей (.m3d + .json от run.py).

  py make_assembly.py out_box --text tasks_box/sborka.txt      # ИИ расставляет детали по описанию
  py make_assembly.py out_box --spec out_box/sborka.json       # расстановка уже готова (без ИИ)

Описание сборки (sborka.txt) — обычным текстом, например:
  «Корпус стоит в начале координат. Крышка лежит на верхнем торце стенок корпуса (z = 53),
   центры совпадают, без поворота.»

ИИ получает список деталей с их габаритами в собственных координатах и возвращает
расстановку: смещение и повороты каждой детали. Расстановка сохраняется в sborka.json
(её можно поправить руками), сборка строится в КОМПАСе, её габарит сверяется с расчётным.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path

import numpy as np
import yaml
from dotenv import load_dotenv

from agent.dsl import normalize
from agent.refgeom import prims_bbox

ROOT = Path(__file__).resolve().parent

SYSTEM = """Ты — инженер-конструктор. Расставь детали в сборке по описанию.
Каждая деталь задана в своих координатах; дан её габарит lo/hi (мм).
Для каждой детали верни смещение pos = [x, y, z] (куда переходит начало координат детали)
и повороты rot = [rx, ry, rz] в градусах вокруг осей X, затем Y, затем Z (по правилу правой руки).
Первая деталь обычно стоит в начале координат без поворота.
Ответ — только JSON: {"analysis": "кратко, как расставил", "parts": [{"file": "...", "pos": [x,y,z], "rot": [rx,ry,rz]}, ...]}
Используй точные числа из описания и габаритов. Не придумывай деталей, которых нет в списке."""


# ---------------------------------------------------------------- геометрия
def rot_matrix(rx, ry, rz):
    a, b, c = (math.radians(v) for v in (rx, ry, rz))
    Rx = np.array([[1, 0, 0], [0, math.cos(a), -math.sin(a)], [0, math.sin(a), math.cos(a)]])
    Ry = np.array([[math.cos(b), 0, math.sin(b)], [0, 1, 0], [-math.sin(b), 0, math.cos(b)]])
    Rz = np.array([[math.cos(c), -math.sin(c), 0], [math.sin(c), math.cos(c), 0], [0, 0, 1]])
    return Rz @ Ry @ Rx


def placed_bbox(lo, hi, pos, rot):
    R = rot_matrix(*rot)
    pts = np.array([[x, y, z] for x in (lo[0], hi[0]) for y in (lo[1], hi[1]) for z in (lo[2], hi[2])])
    q = pts @ R.T + np.array(pos)
    return q.min(axis=0), q.max(axis=0)


def part_info(folder: Path) -> list[dict]:
    out = []
    for m3d in sorted(folder.glob("*.m3d")):
        if re.search(r"_\d{6}$", m3d.stem):
            continue  # запасные копии вида name_121530.m3d
        js = m3d.with_suffix(".json")
        if not js.exists():
            continue
        r = json.loads(js.read_text(encoding="utf-8"))
        if not r.get("spec"):
            continue
        lo, hi = prims_bbox(normalize(r["spec"]))
        out.append({"file": m3d.name, "lo": [round(float(v), 3) for v in lo], "hi": [round(float(v), 3) for v in hi],
                    "text": r.get("text", "").strip()[:400]})
    return out


def expected_bbox(parts, placement):
    by = {p["file"]: p for p in parts}
    los, his = [], []
    for e in placement["parts"]:
        p = by[e["file"]]
        a, b = placed_bbox(p["lo"], p["hi"], e.get("pos", [0, 0, 0]), e.get("rot", [0, 0, 0]))
        los.append(a)
        his.append(b)
    lo, hi = np.min(los, axis=0), np.max(his, axis=0)
    return (hi - lo).tolist()


# ---------------------------------------------------------------- ИИ
def ask_llm(parts, text, cfg) -> dict:
    from agent.llm import LLMClient, RequestLog
    from agent.privacy import Privacy
    from agent.solver import extract_json

    pv = cfg.get("privacy", {})
    llm = LLMClient(cfg, RequestLog(ROOT / cfg.get("log_file", "logs/requests.jsonl")),
                    Privacy.from_config(pv, ROOT))
    lst = "\n".join(f"- {p['file']}: габарит lo={p['lo']} hi={p['hi']}. Описание детали: {p['text']}" for p in parts)
    msgs = [{"role": "system", "content": SYSTEM},
            {"role": "user", "content": f"Детали:\n{lst}\n\nОписание сборки:\n{text.strip()}\n\nВерни только JSON."}]
    names = {p["file"] for p in parts}
    for attempt in range(3):
        raw = llm.chat(msgs, 0.0 if attempt == 0 else 0.3, "assembly")
        try:
            spec = extract_json(raw)
            bad = [e.get("file") for e in spec.get("parts", []) if e.get("file") not in names]
            if bad or not spec.get("parts"):
                raise ValueError(f"неизвестные файлы {bad}" if bad else "пустой список деталей")
            for e in spec["parts"]:
                assert len(e.get("pos", [0, 0, 0])) == 3 and len(e.get("rot", [0, 0, 0])) == 3
            print("ИИ:", spec.get("analysis", ""))
            return spec
        except Exception as ex:
            msgs += [{"role": "assistant", "content": raw},
                     {"role": "user", "content": f"Ошибка: {ex}. Исправь и верни полный JSON."}]
    sys.exit("ИИ не смог составить расстановку — задайте её вручную (--spec)")


# ---------------------------------------------------------------- КОМПАС
def build_in_kompas(folder: Path, placement: dict, out_path: Path):
    import pythoncom
    from win32com.client import Dispatch, gencache

    c3 = gencache.EnsureModule("{2CAF168C-7961-4B90-9DA2-701419BEEFE3}", 0, 1, 0).constants
    api5 = gencache.EnsureModule("{0422828C-F174-495E-AC5D-D31014DBBE87}", 0, 1, 0)
    k5 = api5.KompasObject(Dispatch("Kompas.Application.5")._oleobj_.QueryInterface(
        api5.KompasObject.CLSID, pythoncom.IID_IDispatch))
    doc = k5.Document3D()
    if not doc.Create(False, False):  # False → сборка
        sys.exit("не удалось создать документ сборки")
    top = doc.GetPart(c3.pTop_Part)
    for i, e in enumerate(placement["parts"]):
        path = str((folder / e["file"]).resolve())
        if not doc.SetPartFromFile(path, top, True):
            print(f"   ! не удалось вставить {e['file']}")
            continue
        comp = doc.GetPart(i)
        R = rot_matrix(*e.get("rot", [0, 0, 0]))
        try:
            pl = comp.GetPlacement()
            pl.SetOrigin(*[float(v) for v in e.get("pos", [0, 0, 0])])
            ax, ay = R[:, 0], R[:, 1]
            pl.SetAxes(float(ax[0]), float(ax[1]), float(ax[2]), float(ay[0]), float(ay[1]), float(ay[2]))
            comp.SetPlacement(pl)
            comp.UpdatePlacement()
        except Exception as ex:
            print(f"   ! расстановка {e['file']}: {type(ex).__name__}: {ex}")
        if i == 0:
            try:
                comp.fixedComponent = True
            except Exception:
                pass
        print(f"   + {e['file']}  pos={e.get('pos')}  rot={e.get('rot')}")
    try:
        doc.RebuildDocument()
    except Exception:
        pass
    gab = None
    try:
        g = top.GetGabarit(True, True)
        vals = [x for x in g if isinstance(x, float)][-6:]
        gab = [abs(vals[3] - vals[0]), abs(vals[4] - vals[1]), abs(vals[5] - vals[2])]
    except Exception as ex:
        print(f"   ! габарит сборки: {ex}")
    if not doc.SaveAs(str(out_path.resolve())):
        print(f"   ! не удалось сохранить {out_path} — закройте его в КОМПАСе")
    return gab


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("folder", help="папка с деталями (.m3d + .json)")
    ap.add_argument("--text", help="описание сборки (.txt) — расстановку делает ИИ")
    ap.add_argument("--spec", help="готовая расстановка (.json)")
    ap.add_argument("--out", help="имя файла сборки (.a3d)", default=None)
    a = ap.parse_args()
    folder = Path(a.folder)
    parts = part_info(folder)
    if not parts:
        sys.exit(f"в {folder} нет деталей с .json")
    print("детали:", ", ".join(p["file"] for p in parts))

    if a.spec:
        placement = json.loads(Path(a.spec).read_text(encoding="utf-8"))
    elif a.text:
        load_dotenv(ROOT / ".env")
        cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
        placement = ask_llm(parts, Path(a.text).read_text(encoding="utf-8-sig"), cfg)
        sp = folder / "sborka.json"
        sp.write_text(json.dumps(placement, ensure_ascii=False, indent=2), encoding="utf-8")
        print("расстановка сохранена:", sp)
    else:
        sys.exit("укажите --text (описание для ИИ) или --spec (готовая расстановка)")

    exp = expected_bbox(parts, placement)
    print("ожидаемый габарит сборки:", [round(v, 2) for v in exp])
    out = Path(a.out) if a.out else folder / "sborka.a3d"
    gab = build_in_kompas(folder, placement, out)
    if gab:
        ok = all(abs(x - y) < 0.5 for x, y in zip(sorted(gab), sorted(exp)))
        print("габарит в КОМПАСе:", [round(v, 2) for v in gab], "— совпадает" if ok else "— НЕ совпадает, проверьте расстановку")
    print("сборка:", out.resolve())


if __name__ == "__main__":
    main()
