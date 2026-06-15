import os
import re

VIEWS_DIR = 'views'
MESSAGES_FILE = 'messages.py'

# Read messages.py and build a message text → constant mapping
message_mapping = {}
with open(MESSAGES_FILE, 'r', encoding='utf-8') as f:
    for line in f:
        match = re.match(r'(\w+)\s*=\s*["\'](.*)["\']', line)
        if match:
            key, value = match.groups()
            message_mapping[value] = key

# Regex to find message= "some text" or message= f"some text"
message_pattern = re.compile(r'message\s*=\s*(f?["\'])(.*?)\1')

def update_file(filepath):
    with open(filepath, "r", encoding='utf-8') as file:
        content = file.read()

    modified = False
    imports_added = False

    # Add import if not present
    if 'from messages import *' not in content:
        content = f'from messages import *\n{content}'
        modified = True
        imports_added = True

    # Replace message= with constant if found in mapping
    def replace_message(match):
        prefix, message_text = match.groups()
        constant = message_mapping.get(message_text)
        if constant:
            nonlocal modified
            modified = True
            return f'message={constant}'
        else:
            return match.group(0)  # leave unchanged if not found

    new_content = message_pattern.sub(replace_message, content)

    if modified:
        with open(filepath, "w", encoding='utf-8') as file:
            file.write(new_content)
        print(f"✔️ Updated: {filepath}")
    elif imports_added:
        with open(filepath, "w", encoding='utf-8') as file:
            file.write(content)
        print(f"📌 Import added: {filepath}")

def process_folder(folder_path):
    for root, _, files in os.walk(folder_path):
        for file in files:
            if file.endswith(".py"):
                update_file(os.path.join(root, file))

if __name__ == "__main__":
    process_folder(VIEWS_DIR)
