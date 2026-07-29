import re

with open("templates/index.html", "r") as f:
    content = f.read()

join_panel = """
        <!-- MODERATION BOT DEPLOYMENT -->
        <div class="panel" style="border-top: 4px solid #10b981;">
            <h3 style="color: #10b981;">🛡️ Join Channel (Bot Moderation)</h3>
            <p style="color: #9ca3af; margin-bottom: 15px;">Click the button below to instantly deploy Goddess AI to your active YouTube live stream. It will join as a normal participant.</p>

            <form action="/api/bot/join" method="POST" style="margin-bottom: 15px;">
                <button type="submit" style="background: #10b981; color: white; padding: 12px 20px; border: none; border-radius: 8px; cursor: pointer; font-weight: bold; width: 100%;">
                    ✅ Join Channel
                </button>
            </form>

            {% if request.query_params.get('success') == 'join_success' %}
                <div style="background: rgba(16, 185, 129, 0.1); border: 1px solid #10b981; padding: 12px; border-radius: 8px; color: #10b981; margin-bottom: 15px; font-weight: bold;">
                    ✅ Goddess AI has successfully joined your live chat!
                    <br><br>
                    <strong>IMPORTANT:</strong> For the bot to delete messages, timeout users, or ban trolls, you MUST make it a standard moderator in your YouTube Studio Community Settings.
                </div>
            {% elif request.query_params.get('error') == 'join_not_live' %}
                <div style="background: rgba(245, 158, 11, 0.1); border: 1px solid #f59e0b; padding: 12px; border-radius: 8px; color: #f59e0b; margin-bottom: 15px; font-weight: bold;">
                    ⚠️ We checked your channel, but you are not currently live on YouTube.
                </div>
            {% endif %}
        </div>

        <!-- 1v1 QUEUE MANAGER -->
"""

content = content.replace('<!-- 1v1 QUEUE MANAGER -->', join_panel)

with open("templates/index.html", "w") as f:
    f.write(content)
