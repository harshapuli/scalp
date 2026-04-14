import re

with open('config_v2.py', 'r') as f:
    lines = f.readlines()

in_strategy = False
current_strat = []
strats = []
for i, line in enumerate(lines):
    if line.strip() == '{':
        in_strategy = True
        current_strat = []
    
    if in_strategy:
        current_strat.append((i, line))
    
    if line.strip() == '},' and in_strategy:
        strats.append(current_strat)
        in_strategy = False

for strat in strats:
    text = "".join([l[1] for l in strat])
    if "'category':" in text:
        name = re.search(r"'name':\s*'([^']+)'", text)
        cat = re.search(r"'category':\s*'([^']+)'", text)
        sp = re.search(r"'stop_pct':\s*([\d\.]+)", text)
        tp = re.search(r"'target_pct':\s*([\d\.]+)", text)
        category_name = cat.group(1) if cat else 'N/A'
        if category_name in ['scalp', 'weekly']:
            print(f"{name.group(1)} ({category_name}): STOP={sp.group(1)}, TARGET={tp.group(1)}")
