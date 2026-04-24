class OpenAIClient:
    """
    Stub — not yet active.

    To activate:
      1. pip install openai
      2. Set OPENAI_API_KEY in .env
      3. Set LLM_PROVIDER=openai in .env
      4. Replace the body of call() with:

         import openai
         client = openai.OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
         MODEL = "gpt-4o"  # or preferred model

         response = client.chat.completions.create(
             model=MODEL,
             temperature=temperature,
             messages=[
                 {"role": "system", "content": system},
                 {"role": "user", "content": prompt},
             ],
         )
         return response.choices[0].message.content
    """

    def call(self, prompt: str, system: str, temperature: float = 0.3) -> str:
        raise NotImplementedError("OpenAIClient is a stub — see docstring to activate.")
