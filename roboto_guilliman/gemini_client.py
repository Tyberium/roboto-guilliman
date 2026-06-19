"""Gemini generation via google-genai."""

from __future__ import annotations

from google import genai
from google.genai import types

from roboto_guilliman.config import Settings, get_settings
from roboto_guilliman.gcp_auth import optional_local_credentials
from roboto_guilliman.prompts import SYSTEM_PERSONA, RetrievedChunk, build_user_prompt


class GeminiArbiter:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        credentials = optional_local_credentials()
        client_kwargs: dict[str, object] = {
            "vertexai": True,
            "project": self.settings.gcp_project_id,
            "location": self.settings.gcp_location,
        }
        if credentials is not None:
            client_kwargs["credentials"] = credentials
        self.client = genai.Client(**client_kwargs)

    def answer(self, query: str, chunks: list[RetrievedChunk]) -> str:
        if not chunks:
            return (
                "The provided rules do not cover this specific interaction. "
                "No relevant rule text was retrieved from the index."
            )

        response = self.client.models.generate_content(
            model=self.settings.llm_model,
            contents=build_user_prompt(query, chunks),
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_PERSONA,
                temperature=self.settings.llm_temperature,
                max_output_tokens=self.settings.llm_max_output_tokens,
            ),
        )
        text = response.text
        if not text:
            return "The provided rules do not cover this specific interaction."
        return text.strip()
