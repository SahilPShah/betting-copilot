class GeminiClient:
    """
    Stub — not yet active.

    To activate:
      1. pip install google-generativeai
      2. Set GEMINI_API_KEY in .env
      3. Set LLM_PROVIDER=gemini in .env
      4. Replace the body of call() with:

         import google.generativeai as genai
         MODEL = "gemini-1.5-pro"  # or preferred model

         genai.configure(api_key=os.getenv("GEMINI_API_KEY"))
         model = genai.GenerativeModel(
             model_name=MODEL,
             system_instruction=system,
         )
         response = model.generate_content(
             prompt,
             generation_config=genai.GenerationConfig(temperature=temperature),
         )
         return response.text
    """

    def call(self, prompt: str, system: str, temperature: float = 0.3) -> str:
        raise NotImplementedError("GeminiClient is a stub — see docstring to activate.")
