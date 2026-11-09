#!/usr/bin/env python3
"""
Exploração SOMENTE LEITURA da API do Canvas.

Objetivo: descobrir como os dados da sua instituição realmente chegam
(cursos, planner, notas, arquivos, módulos, avisos, tipos de tarefa).

Uso:
    pip install requests
    cp .env.example .env      # preencha CANVAS_BASE_URL e CANVAS_TOKEN
    python explore_canvas.py --anon

Saídas (pasta canvas_dump/):
    summary.json  -> resumo estrutural. Com --anon, nomes viram "Matéria 1", etc.
                     É ESTE arquivo que você pode me mandar.
    raw_*.json    -> dados brutos completos. NÃO compartilhe, NÃO suba no Git.
"""

import argparse
import json
import os
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

OUT_DIR = Path("canvas_dump")
MAX_PAGES = 5  # limite por endpoint, suficiente para exploração


def load_env(path=".env"):
    """Leitor mínimo de .env (evita depender de python-dotenv)."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


class Canvas:
    def __init__(self, base_url, token):
        self.base = base_url.rstrip("/") + "/api/v1"
        self.s = requests.Session()
        self.s.headers["Authorization"] = f"Bearer {token}"

    def get_all(self, path, params=None):
        """GET paginado (segue o header Link rel=next). Retorna (status, itens)."""
        url = self.base + path
        params = dict(params or {})
        params.setdefault("per_page", 100)
        items, status = [], None
        for _ in range(MAX_PAGES):
            try:
                r = self.s.get(url, params=params, timeout=30)
            except requests.RequestException as e:
                return f"erro de rede: {e}", items
            status = r.status_code
            if r.status_code != 200:
                return status, items
            data = r.json()
            if isinstance(data, list):
                items.extend(data)
            else:
                return status, data  # objeto único
            nxt = r.links.get("next", {}).get("url")
            if not nxt:
                break
            url, params = nxt, None  # a URL 'next' já carrega os parâmetros
        return status, items


def save_raw(name, data):
    OUT_DIR.mkdir(exist_ok=True)
    (OUT_DIR / f"raw_{name}.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def keys_of(items, limit=60):
    """Conjunto de campos presentes no primeiro item (para ver o 'formato')."""
    if isinstance(items, list) and items and isinstance(items[0], dict):
        return sorted(items[0].keys())[:limit]
    return []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--anon", action="store_true",
                    help="anonimiza nomes de matérias no summary.json")
    ap.add_argument("--days", type=int, default=7,
                    help="janela do planner em dias (padrão 7)")
    args = ap.parse_args()

    load_env()
    base, token = os.getenv("CANVAS_BASE_URL"), os.getenv("CANVAS_TOKEN")
    if not base or not token:
        sys.exit("Defina CANVAS_BASE_URL e CANVAS_TOKEN no .env")

    cv = Canvas(base, token)
    summary = {"generated_at": datetime.now(timezone.utc).isoformat()}

    # 0) Teste de autenticação
    st, me = cv.get_all("/users/self/profile")
    summary["auth_status"] = st
    if st != 200:
        print(f"Falha de autenticação (status {st}). Confira URL e token.")
        json.dump(summary, open("summary_error.json", "w"), indent=2)
        sys.exit(1)
    print("✔ Autenticado.")

    # 1) Cursos ativos
    st, courses = cv.get_all("/courses", {
        "enrollment_state": "active",
        "include[]": ["term", "total_scores", "teachers"],
    })
    save_raw("courses", courses)
    summary["courses_status"] = st
    summary["courses_fields"] = keys_of(courses)
    print(f"✔ {len(courses)} curso(s) ativo(s).")

    # 2) Planner (feed unificado de prazos)
    now = datetime.now(timezone.utc)
    st, planner = cv.get_all("/planner/items", {
        "start_date": now.date().isoformat(),
        "end_date": (now + timedelta(days=args.days)).date().isoformat(),
    })
    save_raw("planner", planner)
    summary["planner"] = {
        "status": st,
        "count": len(planner) if isinstance(planner, list) else None,
        "by_type": dict(Counter(i.get("plannable_type") for i in planner))
        if isinstance(planner, list) else None,
        "item_fields": keys_of(planner),
        "plannable_fields": keys_of(
            [i["plannable"] for i in planner if isinstance(i, dict) and "plannable" in i]
        ),
    }
    print(f"✔ Planner: {summary['planner']['count']} item(ns) em {args.days} dias.")

    # 3) Por curso: abas, tarefas, notas, arquivos, módulos, avisos
    summary["courses"] = []
    for idx, c in enumerate(courses, start=1):
        cid = c["id"]
        label = f"Matéria {idx}" if args.anon else c.get("name")
        print(f"… explorando {label}")
        info = {"label": label}
        info["term"] = (c.get("term") or {}).get("name") if not args.anon else "(oculto)"

        # notas vindas do próprio curso (include=total_scores)
        enr = (c.get("enrollments") or [{}])[0]
        info["grade_fields_in_enrollment"] = sorted(
            k for k in enr.keys() if "score" in k or "grade" in k
        )
        info["has_current_score"] = enr.get("computed_current_score") is not None

        # abas visíveis (revela se Arquivos/Módulos/Notas estão habilitados)
        st, tabs = cv.get_all(f"/courses/{cid}/tabs")
        info["tabs"] = (
            [t.get("label") for t in tabs if not t.get("hidden")]
            if isinstance(tabs, list) else f"status {st}"
        )

        # tarefas + minha submissão (mostra tipos: quiz, upload, texto...)
        st, asg = cv.get_all(f"/courses/{cid}/assignments",
                             {"include[]": ["submission"]})
        info["assignments_status"] = st
        if isinstance(asg, list):
            save_raw(f"assignments_{cid}", asg)
            info["assignments_count"] = len(asg)
            info["submission_types"] = dict(Counter(
                t for a in asg for t in (a.get("submission_types") or [])
            ))
            info["quiz_like"] = sum(
                1 for a in asg
                if a.get("is_quiz_assignment") or a.get("quiz_id")
                or "external_tool" in (a.get("submission_types") or [])
            )
            info["graded_by_me"] = sum(
                1 for a in asg
                if (a.get("submission") or {}).get("workflow_state") == "graded"
            )
            info["assignment_fields"] = keys_of(asg)

        # arquivos
        st, files = cv.get_all(f"/courses/{cid}/files")
        info["files_status"] = st
        if isinstance(files, list):
            save_raw(f"files_{cid}", files)
            info["files_count"] = len(files)
            info["file_types"] = dict(Counter(f.get("content-type") for f in files))
            info["file_fields"] = keys_of(files)

        # módulos
        st, mods = cv.get_all(f"/courses/{cid}/modules", {"include[]": ["items"]})
        info["modules_status"] = st
        if isinstance(mods, list):
            save_raw(f"modules_{cid}", mods)
            info["modules_count"] = len(mods)
            info["module_item_types"] = dict(Counter(
                i.get("type") for m in mods for i in (m.get("items") or [])
            ))

        # avisos (últimos 60 dias)
        st, ann = cv.get_all("/announcements", {
            "context_codes[]": [f"course_{cid}"],
            "start_date": (now - timedelta(days=60)).date().isoformat(),
            "end_date": now.date().isoformat(),
        })
        info["announcements_status"] = st
        if isinstance(ann, list):
            info["announcements_count"] = len(ann)

        summary["courses"].append(info)

    OUT_DIR.mkdir(exist_ok=True)
    (OUT_DIR / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\nPronto. Resumo em {OUT_DIR/'summary.json'}")
    print("Dados brutos em canvas_dump/raw_*.json (NÃO compartilhe).")


if __name__ == "__main__":
    main()
