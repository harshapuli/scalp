import re

with open('config_v2.py', 'r') as f:
    content = f.read()

def replacer(match):
    block = match.group(0)
    # Check if this block is actually a strategy by seeing if it has a category
    if "'category': " not in block:
        return block
        
    if "'category': 'scalp'" in block:
        block = re.sub(r"('target_pct':\s*)[\d\.]+", r"\g<1>0.40", block)
    elif "'category': '0dte'" in block:
        block = re.sub(r"('target_pct':\s*)[\d\.]+", r"\g<1>0.80", block)
    return block

# The regex matches exactly one dictionary block from { to },
# assuming no nested dictionaries inside a strategy definition.
new_content = re.sub(r"\{[^{}]*'name':[^{}]*\},", replacer, content)

with open('config_v2.py', 'w') as f:
    f.write(new_content)

print("Targets updated!")
