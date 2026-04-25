import re

with open('analysis.php', 'r', encoding='utf-8') as f:
    content = f.read()

# 1. Add sidebar menu item
sidebar_item_search = """  <div class="sidebar-item" data-tab="control" onclick="switchTab('control',this)">
    <span class="si-icon">🤖</span> Bot Control
    <span class="si-dot off" id="sidebarBotDot"></span>
  </div>"""
sidebar_item_replace = """  <div class="sidebar-item" data-tab="control" onclick="switchTab('control',this)">
    <span class="si-icon">🤖</span> Bot Control
    <span class="si-dot off" id="sidebarBotDot"></span>
  </div>
  <div class="sidebar-item" data-tab="autopilot" onclick="switchTab('autopilot',this)">
    <span class="si-icon">🚀</span> Autopilot
  </div>"""

content = content.replace(sidebar_item_search, sidebar_item_replace)

# 2. Extract the Systematic Autopilot card from Left Column
ap_card_pattern = re.compile(r'(\s*<div class="card" style="margin-top:16px">\s*<div class="card-header"><h3>🤖 Systematic Autopilot</h3></div>.*?</div>\s*</div>\s*</div>)', re.DOTALL)
ap_card_match = ap_card_pattern.search(content)
if not ap_card_match:
    print("Could not find Autopilot Card")
    exit(1)
ap_card_html = ap_card_match.group(1)

content = content.replace(ap_card_html, '')

# 3. Extract the Right Column cards (Autopilot Logs and Sprint History)
right_col_pattern = re.compile(r'(\s*<div class="card" style="margin-bottom:16px">\s*<div class="card-header">\s*<h3>Autopilot Logs</h3>.*?<div id="apResEmpty".*?</div>\s*</div>\s*</div>)', re.DOTALL)
right_col_match = right_col_pattern.search(content)
if not right_col_match:
    print("Could not find Right Col Cards")
    exit(1)
right_col_html = right_col_match.group(1)

content = content.replace(right_col_html, '')

# 4. Construct the new tab-autopilot
new_tab = f"""

    <!-- ═══════════════ AUTOPILOT TAB ═══════════════ -->
    <div class="tab-content" id="tab-autopilot">
      <div class="page-header">
        <h2>Autopilot</h2>
        <p>Configure, manage, and monitor the Systematic Autopilot sprints</p>
      </div>

      <div class="ctrl-layout">
        <!-- LEFT COLUMN -->
        <div>
{ap_card_html}
        </div>

        <!-- RIGHT COLUMN -->
        <div>
{right_col_html}
        </div>
      </div>
    </div>
"""

# Insert new tab right after tab-control ends
tab_training_index = content.find('<!-- ═══════════════ ML TRAINING TAB ═══════════════ -->')
content = content[:tab_training_index] + new_tab + content[tab_training_index:]

# Fix the CSS margin of the ap_card to not have margin-top:16px since it's the first element now
content = content.replace('<div class="card" style="margin-top:16px">\n            <div class="card-header"><h3>🤖 Systematic Autopilot</h3></div>', '<div class="card">\n            <div class="card-header"><h3>🤖 Systematic Autopilot</h3></div>')

with open('analysis.php', 'w', encoding='utf-8') as f:
    f.write(content)
print("Done")
