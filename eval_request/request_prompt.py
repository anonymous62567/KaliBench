#!/usr/bin/env python3
# request_prompt.py
"""
Generate requests.jsonl and labels.jsonl in three modes:
  - unrestricted : embed only query in the prompt (models should identify the tool themselves)
  - restricted   : additionally embed a subset of tool names that contain the real tool (but no usage manuscript is cascaded for each tool)
  - hinted       : additionally embed a subset of tools with usage snippets

Usage examples (PowerShell single-line recommended):

--------------------------------------Unrestricted--------------------------------------

# replace ".\" with your absolute route, replace "models/meta-llama/Llama-3.3-70B-Instruct" with your model's storage route
# out_labels: output label file is the same for 3 modes

python .\request_prompt.py --mode unrestricted --input ".\KaliBench_Eval_4097.jsonl" --subtools ".\Kali_Tool_Subtools_UsageCode.jsonl" --out_requests "unrestricted_requests.jsonl" --out_labels "labels.jsonl" --model "models/meta-llama/Llama-3.3-70B-Instruct" --temperature 0.2 --tools_per_chunk 20 --max_usage_chars 800 --include_usage_in_prompt false

----------------------------------------Restricted--------------------------------------

# replace ".\" with your absolute route, replace "models/meta-llama/Llama-3.3-70B-Instruct" with your model's storage route
# out_labels: output label file is the same for 3 modes

python .\request_prompt.py --mode restricted --input ".\KaliBench_Eval_4097.jsonl" --subtools ".\Kali_Tool_Subtools_UsageCode.jsonl" --out_requests "restricted_requests.jsonl" --out_labels "labels.jsonl" --model "models/meta-llama/Llama-3.3-70B-Instruct" --temperature 0.2 --tools_per_chunk 20 --max_usage_chars 800 --include_usage_in_prompt false


----------------Hinted (Include usage manuscript with include_usage_in_prompt true)------------

# replace ".\" with your absolute route, replace "models/meta-llama/Llama-3.3-70B-Instruct" with your model's storage route
# out_labels: output label file is the same for 3 modes

python .\request_prompt.py --mode hinted --input ".\KaliBench_Eval_4097.jsonl" --subtools ".\Kali_Tool_Subtools_UsageCode.jsonl" --out_requests "hinted_requests.jsonl" --out_labels "labels.jsonl" --model "models/meta-llama/Llama-3.3-70B-Instruct" --temperature 0.2 --tools_per_chunk 20 --max_usage_chars 800 --include_usage_in_prompt true

"""

import json
import pathlib
import argparse
from typing import Dict, List, Optional, Iterable, Any

# ----------------------
# Prompts
# ----------------------
system_prompt_restricted = """You are a cybersecurity function-calling AI model.
TASK:
Given a USER QUERY and a list of allowed tools provided in the user message, generate the single most accurate shell command that solves the query using ONLY the allowed tools.

REQUIREMENTS:
1. Use the correct command-line tool from the allowed list.
2. Include all required optional arguments (flags beginning with '-' or '--') necessary to accomplish the task.
3. Correctly pair option keys and values (e.g., `--port 80`, `-A INPUT`, or `--flag=value`).
4. Preserve and include any positional arguments (e.g., target IPs, filenames, interfaces).
5. Do NOT invent flags/options that do not exist for real Kali/Linux tools.
6. DO NOT include explanations, commentary, or any text outside the prescribed output structure.
7. Note: scoring will penalize missing optional arguments, incorrect option→value pairs, or omitted positional arguments.

OUTPUT FORMAT (exactly):
<output>
[command]
</output>"""

system_prompt_unrestricted = """You are a cybersecurity function-calling AI model.
You have access to the following tool:
<tools>
[{'type':'function','function':{'name':'run_terminal','description':'Execute a shell command in a Kali/Linux terminal and return stdout, stderr, and exit code.'}}]
</tools>

TASK:
Given a USER QUERY, generate the single most accurate shell command using any appropriate Kali/Linux tool(s) to solve the query.

REQUIREMENTS:
1. Use the correct command-line tool(s) appropriate for the task.
2. Include all required optional arguments (flags beginning with '-' or '--') necessary to accomplish the task.
3. Correctly pair option keys and values (e.g., `--port 80`, `-A INPUT`, or `--flag=value`).
4. Preserve and include any positional arguments (e.g., IPs, filenames, interfaces).
5. Do NOT invent flags/options that do not exist for real Kali/Linux tools.
6. DO NOT include explanations, commentary, or any text outside the prescribed output structure.
7. Note: scoring will penalize missing optional arguments, incorrect option→value pairs, or omitted positional arguments.

OUTPUT FORMAT (exactly):
<output>
[command]
</output>"""

# ----------------------
# Helpers
# ----------------------
def str2bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    s = str(v).strip().lower()
    if s in ("yes", "true", "t", "1", "y"):
        return True
    if s in ("no", "false", "f", "0", "n"):
        return False
    raise argparse.ArgumentTypeError("Boolean value expected (true/false).")

def parse_args():
    p = argparse.ArgumentParser(description="Generate requests.jsonl and labels.jsonl in unrestricted/restricted/hinted modes.")
    p.add_argument("--mode", type=str, choices=["unrestricted", "restricted", "hinted"], default="hinted",
                   help="Operation mode: unrestricted / restricted / hinted (default: hinted).")
    p.add_argument("--input", type=str, required=True,
                   help="Input JSONL file. Each line must be a JSON object with fields like custom_id, query, tool_name, ground_truth_command, etc.")
    p.add_argument("--subtools", type=str, default="kali_tools_subtools.jsonl",
                   help="Subtools JSONL with fields like subtool/tool/title and usage_code/manuscript/usage.")
    p.add_argument("--out_requests", type=str, default="requests.jsonl",
                   help="Output requests.jsonl path.")
    p.add_argument("--out_labels", type=str, default="labels.jsonl",
                   help="Output labels.jsonl path.")
    p.add_argument("--model", type=str, required=True,
                   help="Model identifier for request body.")
    p.add_argument("--temperature", type=float, default=0.2)
    p.add_argument("--tools_per_chunk", type=int, default=20)
    p.add_argument("--max_usage_chars", type=int, default=800,
                   help="Max chars of usage snippet. Set 0 to disable usage (even if hinted mode).")
    p.add_argument("--include_usage_in_prompt", type=str2bool, default=False,
                   help="Whether to include usage snippets (true/false). In 'hinted' mode this controls injection; ignored in unrestricted.")
    p.add_argument("--encoding", type=str, default="utf-8")
    return p.parse_args()

def chunk_list(lst: List[str], n: int) -> Iterable[List[str]]:
    for i in range(0, len(lst), n):
        yield lst[i:i+n]

def load_tool_usages(jsonl_path: pathlib.Path, encoding: str = "utf-8") -> Dict[str, str]:
    tool_map: Dict[str, str] = {}
    if not jsonl_path.exists():
        print(f"[WARN] subtools file not found: {jsonl_path}")
        return tool_map
    with jsonl_path.open("r", encoding=encoding) as fh:
        for lineno, line in enumerate(fh, start=1):
            s = line.strip()
            if not s: continue
            try:
                rec = json.loads(s)
            except Exception as e:
                print(f"[WARN] failed to parse subtools jsonl line {lineno}: {e}")
                continue
            sub = rec.get("subtool") or rec.get("tool") or rec.get("title") or rec.get("name") or ""
            usage = rec.get("usage_code") or rec.get("manuscript") or rec.get("usage") or rec.get("description") or ""
            if not sub: continue
            tool_map[sub.strip().lower()] = usage or ""
    return tool_map

def make_allowed_str_with_usage(tool_chunk: List[str],
                                usage_map_for_chunk: Dict[str, str],
                                include_usage: bool = False,
                                max_usage_chars: Optional[int] = 800) -> str:
    lines: List[str] = []
    for t in tool_chunk:
        lines.append(f"- {t}")
        if include_usage:
            if max_usage_chars == 0:
                continue
            usage = usage_map_for_chunk.get(t, "")
            if usage:
                if max_usage_chars is not None and max_usage_chars > 0 and len(usage) > max_usage_chars:
                    usage_snip = usage[:max_usage_chars].rstrip() + "\n...(truncated)"
                else:
                    usage_snip = usage
                for ul in usage_snip.strip().splitlines():
                    lines.append("  " + ul)
    return "\n".join(lines)

# ----------------------
# Input reader (JSONL only)
# ----------------------
def read_jsonl(path: pathlib.Path, encoding: str = "utf-8") -> List[Dict[str, Any]]:
    recs: List[Dict[str, Any]] = []
    with path.open("r", encoding=encoding) as f:
        for lineno, line in enumerate(f, start=1):
            s = line.strip()
            if not s: continue
            try:
                recs.append(json.loads(s))
            except Exception as e:
                print(f"[WARN] failed to parse jsonl line {lineno}: {e}")
    return recs

# ----------------------
# Main
# ----------------------
def main():
    args = parse_args()
    INPATH = pathlib.Path(args.input)
    REQS = pathlib.Path(args.out_requests)
    LABS = pathlib.Path(args.out_labels)
    SUBTOOLS_PATH = pathlib.Path(args.subtools)
    encoding = args.encoding

    # Input must be JSONL for all modes
    if not INPATH.exists():
        print(f"[ERROR] input not found: {INPATH}")
        return

    records = read_jsonl(INPATH, encoding=encoding)
    if not records:
        print("[ERROR] No valid input records in JSONL.")
        return

    # Unrestricted mode: embed query and do NOT include allowed tools
    if args.mode == "unrestricted":
        print(f"[INFO] Mode=unrestricted: will embed query in user prompt and NOT include allowed tools.")
        with REQS.open("w", encoding=encoding) as req_f, LABS.open("w", encoding=encoding) as lab_f:
            for idx, row in enumerate(records, start=1):
                custom_id = (row.get("custom_id") or f"row-{idx}").strip()
                q = (row.get("query") or row.get("prompt") or "").strip()
                if not q:
                    # fallback to first non-empty string field
                    for v in row.values():
                        if isinstance(v, str) and v.strip():
                            q = v.strip()
                            break
                body = {
                    "model": args.model,
                    "temperature": args.temperature,
                    "messages": [
                        {"role": "system", "content": system_prompt_unrestricted},
                        {"role": "user", "content": f'USER QUERY: \"{q}\"\n\nInstructions:\n- Generate a single shell command that best solves the USER QUERY. You may use any appropriate Kali/Linux tool.\n- Include ALL required optional arguments (flags starting with \'-\' or \'--\'), correct option→value pairs, and any positional arguments.\n- Do NOT invent non-existent flags or options.\n- Output EXACTLY the command inside <output>...</output> and nothing else.\n\nReturn ONLY the command inside <output>...</output>'}
                    ]
                }
                req_obj = {"custom_id": custom_id, "method": "POST", "url": "/v1/chat/completions", "body": body}
                req_f.write(json.dumps(req_obj, ensure_ascii=False) + "\n")

                label = {
                    "custom_id": custom_id,
                    "query": q,
                    "tool_name": row.get("tool_name") or row.get("tool") or ""
                }
                for k in ("ground_truth_command", "optional_args", "positional_args", "option_kv"):
                    if k in row:
                        try:
                            label[k] = json.loads(row[k])
                        except Exception:
                            label[k] = row[k]
                    else:
                        label[k] = [] if k in ("optional_args", "positional_args") else ({} if k == "option_kv" else "")
                lab_f.write(json.dumps(label, ensure_ascii=False) + "\n")
        print("✅ Done. Generated unrestricted requests:", REQS, "and labels:", LABS)
        return

    # For restricted/hinted modes: build tool list, chunking and usage map
    tools: List[str] = []
    for rec in records:
        tool = (rec.get("tool_name") or rec.get("tool") or "").strip()
        if tool and tool not in tools:
            tools.append(tool)
    tools = sorted(set(tools))
    allowed_chunks = list(chunk_list(tools, args.tools_per_chunk))
    print(f"[INFO] Found {len(tools)} unique tools -> {len(allowed_chunks)} chunks (tools_per_chunk={args.tools_per_chunk})")

    tool_usage_map = load_tool_usages(SUBTOOLS_PATH, encoding=encoding)
    print(f"[INFO] Loaded usage texts for {len(tool_usage_map)} subtools from {SUBTOOLS_PATH}")

    chunk_usage_map: List[Dict[str, str]] = []
    for chunk in allowed_chunks:
        usage_for_chunk: Dict[str, str] = {}
        for t in chunk:
            key = (t or "").strip().lower()
            usage = tool_usage_map.get(key, "")
            if args.max_usage_chars and args.max_usage_chars > 0 and usage and len(usage) > args.max_usage_chars:
                usage_trunc = usage[:args.max_usage_chars].rstrip() + "\n...(truncated)"
            else:
                usage_trunc = usage
            usage_for_chunk[t] = usage_trunc
        chunk_usage_map.append(usage_for_chunk)

    with REQS.open("w", encoding=encoding) as req_f, LABS.open("w", encoding=encoding) as lab_f:
        for idx, row in enumerate(records, start=1):
            custom_id = (row.get("custom_id") or f"row-{idx}").strip()
            q = (row.get("query") or "").strip()
            tool = (row.get("tool_name") or row.get("tool") or "").strip()

            found_chunk_index = None
            for ci, c in enumerate(allowed_chunks):
                norm_chunk = [x.strip().lower() for x in c]
                if tool.strip().lower() in norm_chunk:
                    found_chunk_index = ci
                    break

            if found_chunk_index is None:
                found_chunk_index = 0
                tool_chunk = allowed_chunks[0] if allowed_chunks else []
                usage_map_for_chunk = chunk_usage_map[0] if chunk_usage_map else {}
            else:
                tool_chunk = allowed_chunks[found_chunk_index]
                usage_map_for_chunk = chunk_usage_map[found_chunk_index]

            include_usage = False
            if args.mode == "hinted":
                include_usage = args.include_usage_in_prompt and (args.max_usage_chars != 0)

            allowed_str = make_allowed_str_with_usage(
                tool_chunk,
                usage_map_for_chunk,
                include_usage=include_usage,
                max_usage_chars=(args.max_usage_chars if args.max_usage_chars and args.max_usage_chars > 0 else None)
            )

            if args.mode == "restricted":
                user_prompt = f"""
Allowed tools (subset):
{allowed_str}

USER QUERY: "{q}"

Instructions:
- Generate a single shell command that solves the USER QUERY using ONLY the allowed tools listed above.
- Include ALL required optional arguments (flags starting with '-' or '--'), correct option→value pairs, and any positional arguments.
- Do NOT invent non-existent flags or options.
- Output EXACTLY the command inside <output>...</output> and nothing else.

Return ONLY the command inside <output>...</output>"""
                system_prompt = system_prompt_restricted
            else:  # hinted
                user_prompt = f"""
Allowed tools (subset):
{allowed_str}

USER QUERY: "{q}"

Instructions:
- Generate a single shell command that solves the USER QUERY using ONLY the allowed tools listed above.
- Use the usage examples above to choose the correct tool and flags, and include ALL required optional arguments (flags starting with '-' or '--'), correct option→value pairs, and any positional arguments.
- Do NOT invent non-existent flags or options.
- Output EXACTLY the command inside <output>...</output> and nothing else.

Return ONLY the command inside <output>...</output>"""
                system_prompt = system_prompt_restricted

            body = {
                "model": args.model,
                "temperature": args.temperature,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ]
            }
            req_obj = {"custom_id": custom_id, "method": "POST", "url": "/v1/chat/completions", "body": body}
            req_f.write(json.dumps(req_obj, ensure_ascii=False) + "\n")

            label = {
                "custom_id": custom_id,
                "query": q,
                "tool_name": tool,
                "ground_truth_command": row.get("ground_truth_command") or ""
            }
            for k in ("optional_args", "positional_args", "option_kv"):
                if k in row:
                    try:
                        label[k] = json.loads(row[k])
                    except Exception:
                        label[k] = row[k]
                else:
                    label[k] = [] if k != "option_kv" else {}
            lab_f.write(json.dumps(label, ensure_ascii=False) + "\n")

    print(f"✅ Done. Generated {args.mode} requests:", REQS, "and labels:", LABS)

if __name__ == "__main__":
    main()