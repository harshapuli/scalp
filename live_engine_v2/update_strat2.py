import re

with open('config_v2.py', 'r') as f:
    content = f.read()

def replacer(match):
    block = match.group(0)
    if "'category': 'scalp'" in block:
        block = re.sub(r"('target_pct':\s*)[\d\.]+", r"\g<1>0.20", block)
        block = re.sub(r"('stop_pct':\s*)[\d\.]+", r"\g<1>0.10", block)
    elif "'category': 'weekly'" in block:
        block = re.sub(r"('target_pct':\s*)[\d\.]+", r"\g<1>0.50", block)
        block = re.sub(r"('stop_pct':\s*)[\d\.]+", r"\g<1>0.25", block)
    return block

new_content = re.sub(r"\{[^{}]*'name':[^{}]*\},", replacer, content)

with open('config_v2.py', 'w') as f:
    f.write(new_content)

print("Updated config_v2.py for scalps and weeklies!")
