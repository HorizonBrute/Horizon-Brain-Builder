#!/usr/bin/env python3
"""add_read_paths.py <template> <comment> <pattern> [<pattern> ...]

Idempotently add entries to the `$is_read` allow-list map in an nginx.conf.template.
Each <pattern> is the nginx map regex key WITHOUT the trailing `1;`, e.g.
  '~^GET:/api/v2/auth/identity/?(\\?.*)?$'
Inserted just before the closing brace of `map $request_method:$request_uri $is_read {`.
Re-running is a no-op. Fails loud if the map block isn't found.
"""
import sys, re

tpl, comment, pats = sys.argv[1], sys.argv[2], sys.argv[3:]
s = open(tpl, encoding="utf-8").read()
m = re.search(r"map \$request_method:\$request_uri \$is_read \{.*?\n(\s*)\}", s, re.S)
assert m, "is_read map block not found (template shape changed?)"
close_ws_start = m.start(1)
ins, added = "", []
for p in pats:
    if p in s:
        continue
    ins += f'        "{p}"  1;   # {comment}\n'
    added.append(p)
if ins:
    s = s[:close_ws_start] + ins + s[close_ws_start:]
    open(tpl, "w", encoding="utf-8", newline="\n").write(s)
    print(f"ADDED {added} -> {tpl}")
else:
    print(f"NOOP (already present) {tpl}")
