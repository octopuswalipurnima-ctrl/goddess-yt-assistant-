import re

with open("app/services/modration/gemini_client.py", "r") as f:
    content = f.read()

import_statement = "from app.database.models import DecisionCache, SystemState"
content = content.replace("from app.database.models import DecisionCache", import_statement)

generate_logic = """
        try:
            # API Cap Check
            sys_state = self.db.query(SystemState).first()
            if sys_state and sys_state.gemini_api_calls >= sys_state.gemini_api_cap:
                return {
                    "classification": "CapExceeded",
                    "severity": "Low",
                    "confidence": 100,
                    "recommended_action": "Safe",
                    "reason": "Gemini API cap exceeded. Defaulting to Safe."
                }

            # Modern structured generation call with mandatory JSON Schema response configuration
            response = client.models.generate_content(
                model=self.model_name,
                contents=user_content,
                config=types.GenerateContentConfig(
                    system_instruction=system_instruction,
                    response_mime_type="application/json",
                    temperature=0.1, # Keep decisions deterministic and highly reliable
                    max_output_tokens=150
                )
            )

            if sys_state:
                sys_state.gemini_api_calls += 1
                self.db.commit()
"""
content = content.replace("""
        try:
            # Modern structured generation call with mandatory JSON Schema response configuration
            response = client.models.generate_content(
                model=self.model_name,
                contents=user_content,
                config=types.GenerateContentConfig(
                    system_instruction=system_instruction,
                    response_mime_type="application/json",
                    temperature=0.1, # Keep decisions deterministic and highly reliable
                    max_output_tokens=150
                )
            )""", generate_logic)

with open("app/services/modration/gemini_client.py", "w") as f:
    f.write(content)
