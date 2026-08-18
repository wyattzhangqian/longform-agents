"""SkillHub 公开 API 客户端 — https://skillhub.cn/skills"""

from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import aiohttp

SKILLHUB_API_BASE = "https://api.skillhub.cn"
DEFAULT_TIMEOUT = aiohttp.ClientTimeout(total=60)


@dataclass
class SkillHubEntry:
    slug: str
    name: str
    version: str
    description: str
    description_zh: str
    downloads: int
    stars: int
    installs: int
    category: str
    source: str
    homepage: str
    owner_name: str

    @classmethod
    def from_api(cls, raw: dict) -> "SkillHubEntry":
        return cls(
            slug=raw.get("slug") or "",
            name=raw.get("name") or raw.get("slug") or "",
            version=str(raw.get("version") or "1.0.0"),
            description=raw.get("description") or "",
            description_zh=raw.get("description_zh") or "",
            downloads=int(raw.get("downloads") or 0),
            stars=int(raw.get("stars") or 0),
            installs=int(raw.get("installs") or 0),
            category=raw.get("category") or "",
            source=raw.get("source") or "",
            homepage=raw.get("homepage") or "",
            owner_name=raw.get("ownerName") or "",
        )


class SkillHubClient:
    def __init__(self, base_url: str = SKILLHUB_API_BASE):
        self.base_url = base_url.rstrip("/")

    async def fetch_skills_page(
        self,
        *,
        page: int = 1,
        page_size: int = 50,
        sort_by: str = "downloads",
        order: str = "desc",
        keyword: str = "",
        category: str = "",
    ) -> Tuple[List[SkillHubEntry], int]:
        params = {
            "page": str(page),
            "pageSize": str(page_size),
            "sortBy": sort_by,
            "order": order,
        }
        if keyword.strip():
            params["keyword"] = keyword.strip()
        if category:
            params["category"] = category

        url = f"{self.base_url}/api/skills"
        async with aiohttp.ClientSession(timeout=DEFAULT_TIMEOUT) as session:
            async with session.get(url, params=params) as resp:
                resp.raise_for_status()
                payload = await resp.json()
        if payload.get("code") != 0:
            raise RuntimeError(payload.get("message") or "SkillHub 列表接口错误")

        data = payload.get("data") or {}
        items = [SkillHubEntry.from_api(x) for x in (data.get("skills") or [])]
        total = int(data.get("total") or 0)
        return items, total

    async def fetch_top_skills(
        self,
        limit: int = 100,
        *,
        sort_by: str = "downloads",
    ) -> List[SkillHubEntry]:
        out: List[SkillHubEntry] = []
        page = 1
        page_size = min(50, limit)
        while len(out) < limit:
            batch, _ = await self.fetch_skills_page(
                page=page,
                page_size=page_size,
                sort_by=sort_by,
                order="desc",
            )
            if not batch:
                break
            out.extend(batch)
            if len(batch) < page_size:
                break
            page += 1
        return out[:limit]

    async def download_skill_zip(self, slug: str) -> bytes:
        url = f"{self.base_url}/api/v1/download"
        async with aiohttp.ClientSession(timeout=DEFAULT_TIMEOUT) as session:
            async with session.get(url, params={"slug": slug}) as resp:
                resp.raise_for_status()
                return await resp.read()

    async def extract_skill_md(self, slug: str) -> str:
        raw = await self.download_skill_zip(slug)
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            for name in ("SKILL.md", "skill.md", "SKILL.MD"):
                if name in zf.namelist():
                    return zf.read(name).decode("utf-8", errors="replace")
            md_files = [n for n in zf.namelist() if n.lower().endswith(".md")]
            if not md_files:
                raise ValueError(f"{slug} 压缩包内无 SKILL.md")
            return zf.read(md_files[0]).decode("utf-8", errors="replace")

    def catalog_meta(self, entry: SkillHubEntry) -> Dict[str, Any]:
        return {
            "provider": "skillhub",
            "slug": entry.slug,
            "version": entry.version,
            "downloads": entry.downloads,
            "stars": entry.stars,
            "installs": entry.installs,
            "category": entry.category,
            "homepage": entry.homepage,
            "owner": entry.owner_name,
            "source": entry.source,
            "url": f"https://skillhub.cn/skills/{entry.slug}",
        }
