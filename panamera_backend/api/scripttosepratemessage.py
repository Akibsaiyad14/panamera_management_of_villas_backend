import os
import re

VIEWS_DIR = 'views'
OUTPUT_FILE = 'messages.py'

# Regex pattern to capture message="..." and message=f"..."
message_pattern = re.compile(r'message\s*=\s*(f?["\'])(.*?)\1')

found_messages = set()

# Walk through all .py files in the views directory
for root, dirs, files in os.walk(VIEWS_DIR):
    for file in files:
        if file.endswith('.py'):
            file_path = os.path.join(root, file)
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()
                matches = message_pattern.findall(content)
                for match in matches:
                    message_text = match[1]
                    found_messages.add(message_text)

# Display found messages
print("Found messages:")
for msg in sorted(found_messages):
    print(f'- {msg}')

# Optional: write to a messages.py file
with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
    f.write('# Auto-generated messages file\n\n')
    for msg in sorted(found_messages):
        key = msg.upper().replace(" ", "_").replace("-", "_").replace(":", "").replace(".", "").replace("/", "_")
        key = re.sub(r'[^A-Z0-9_]', '', key)  # Remove any other non-alphanum characters
        if len(key) > 50:
            key = key[:50]  # Optional: truncate long keys
        f.write(f'{key} = "{msg}"\n')

print(f'\nMessages written to {OUTPUT_FILE}')
