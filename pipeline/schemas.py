"""
Schema definitions for the script generation stage (M2).
The LLM's JSON output is validated against this — malformed output
triggers a retry rather than silently corrupting the pipeline.
"""

from typing import Literal
from pydantic import BaseModel, Field


class Scene(BaseModel):
    role: Literal["hook", "point", "takeaway", "cta"]
    text: str = Field(min_length=1)
    keywords: list[str] = Field(min_length=1, max_length=6)
    est_seconds: float = Field(gt=0)


class Script(BaseModel):
    title: str = Field(min_length=1, max_length=90)
    description: str = Field(min_length=1)
    tags: list[str] = Field(min_length=1)
    scenes: list[Scene] = Field(min_length=3)

    def full_text(self) -> str:
        """Concatenate all scene text — used for word counting and TTS input."""
        return " ".join(scene.text for scene in self.scenes)