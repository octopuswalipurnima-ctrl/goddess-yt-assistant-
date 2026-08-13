import sys
import os
import importlib.metadata

# Safely append parent directory to Python path to import app modules
current_dir = os.path.dirname(os.path.abspath(__file__))
parent_dir = os.path.dirname(current_dir)
sys.path.insert(0, parent_dir)

def main():
    print("=== GEMINI API DIAGNOSTIC START ===")
    
    # 1. Print Python and SDK versions
    print(f"Python Version: {sys.version.split()[0]}")
    try:
        genai_version = importlib.metadata.version("google-genai")
        print(f"google-genai Version: {genai_version}")
    except importlib.metadata.PackageNotFoundError:
        print("google-genai Version: NOT FOUND (Package not installed)")
        return

    # 2. Safely load the API Key using the existing credential manager
    api_key = None
    cred_identifier = "None"
    try:
        from app.services.common.credential_manager import gemini_cred_manager
        cred = gemini_cred_manager.get_healthy_credential()
        if cred:
            api_key = cred.secret
            cred_identifier = cred.identifier
    except ImportError:
        # Fallback if run completely detached
        api_key = os.environ.get("GEMINI_API_KEY")
        if api_key:
            cred_identifier = "ENV_GEMINI_API_KEY"

    print(f"Gemini Key Found: {'YES' if api_key else 'NO'}")
    print(f"Credential Identifier: {cred_identifier}")

    if not api_key:
        print("🛑 Stopping diagnostic: No API key could be loaded.")
        return

    # 3. Initialize Modern SDK Client
    try:
        from google import genai
        client = genai.Client(api_key=api_key)
    except ImportError as e:
        print(f"🛑 Failed to import google.genai: {e}")
        return

    # 4. Check Available Models
    print("\n--- Listing Authorized Models ---")
    target_model_name = "gemini-2.5-flash"
    target_model_found = False
    supports_generate = False
    available_models = []

    try:
        models = client.models.list()
        for m in models:
            available_models.append(m.name)
            
            # Check if our specific model is in the allowed list
            if target_model_name in m.name:
                target_model_found = True
                
                # Check if it specifically supports generateContent
                supported_actions = getattr(m, 'supported_actions', [])
                if supported_actions and "generateContent" in supported_actions:
                    supports_generate = True
                elif not supported_actions:
                    # If the property isn't returned, assume true for testing
                    supports_generate = True

        print(f"Total Accessible Models: {len(available_models)}")
        print(f"Sample Accessible Models: {available_models[:10]}")
        
    except Exception as e:
        print("🛑 Failed to list models.")
        print(f"Exception Type: {type(e).__name__}")
        safe_error = str(e).replace(api_key, "[REDACTED_API_KEY]")
        print(f"Safe Error Message: {safe_error}")
        status_code = getattr(e, 'code', getattr(e, 'status_code', getattr(e, 'status', 'Unknown')))
        print(f"HTTP/API Status: {status_code}")
        return

    print(f"\nTarget Model '{target_model_name}' Found: {'YES' if target_model_found else 'NO'}")
    print(f"Supports generateContent: {'YES' if supports_generate else 'NO'}")

    # 5. Minimal Generation Test
    if target_model_found:
        print(f"\n--- Testing generateContent ({target_model_name}) ---")
        try:
            response = client.models.generate_content(
                model=target_model_name,
                contents="Reply with exactly: GEMINI_TEST_OK"
            )
            
            print("Status: SUCCESS ✅")
            print(f"Model Used: {target_model_name}")
            print(f"Response Text: {response.text.strip() if response.text else 'EMPTY RESPONSE'}")
            
        except Exception as e:
            print("Status: FAILURE ❌")
            print(f"Model Used: {target_model_name}")
            print(f"Exception Type: {type(e).__name__}")
            status_code = getattr(e, 'code', getattr(e, 'status_code', getattr(e, 'status', 'Unknown')))
            print(f"HTTP/API Status: {status_code}")
            
            safe_error = str(e).replace(api_key, "[REDACTED_API_KEY]")
            print(f"Safe Error Message: {safe_error}")
    else:
        print(f"\n🛑 Skipping generateContent test because {target_model_name} is completely unavailable to this API key/project.")

    print("\n=== DIAGNOSTIC COMPLETE ===")

if __name__ == '__main__':
    main()