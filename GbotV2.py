"""
Random gerecht-keuze generator (AH Allerhande)
- Scraped recepten (titel + URL) uit de zoekresultatenpagina’s
- Kiest daarna willekeurig 1 gerecht

Let op:
- Respecteer AH/Allerhande ToS en robots.txt
- Gebruik een nette User-Agent en een kleine delay (zit erin)
"""

from __future__ import annotations

import json
import os
import random
import time
from dataclasses import dataclass
from typing import Iterable
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

import xml.etree.ElementTree as ET

from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


BASE = "https://www.ah.nl"
SITEMAP_URL = "https://www.ah.nl/sitemaps/entities/allerhande/recipes.xml"

# Filters die jij wil (matchen we op de receptpagina zelf)
FILTER_KOOKTECHNIEK = "koken"
FILTER_MENUGANG = "hoofdgerecht"


@dataclass(frozen=True)
class Recipe:
    title: str
    url: str


def _get_recipe_urls_from_sitemap(
    session: requests.Session,
    sitemap_url: str,
    max_urls: int,
    timeout_s: float,
) -> list[str]:
    """Lees recipe-URLs uit de AH Allerhande sitemap (streaming)."""
    resp = session.get(
        sitemap_url,
        timeout=timeout_s,
        stream=True,
        headers={"Accept-Encoding": "gzip, deflate"},
    )
    resp.raise_for_status()

    urls: list[str] = []

    # Zorg dat urllib3/requests de (gzip) content ook echt decodeert als we streamen.
    # Zonder dit kan ET.iterparse binaire/compressed bytes krijgen -> ParseError op line 1 col 0.
    resp.raw.decode_content = True

    try:
        # Streaming parse zodat we niet het hele XML document in memory hoeven
        context = ET.iterparse(resp.raw, events=("end",))
        for event, elem in context:
            if elem.tag.endswith("loc") and elem.text:
                loc = elem.text.strip()
                if "/allerhande/recept/" in loc:
                    urls.append(loc)
                    if len(urls) >= max_urls:
                        break
            elem.clear()
        return urls

    except ET.ParseError:
        # Fallback: parse het volledige (door requests gedecompresseerde) document
        # Dit is minder memory-vriendelijk, maar wel robuust.
        text = resp.text
        root = ET.fromstring(text)
        for loc_el in root.iter():
            if loc_el.tag.endswith("loc") and loc_el.text:
                loc = loc_el.text.strip()
                if "/allerhande/recept/" in loc:
                    urls.append(loc)
                    if len(urls) >= max_urls:
                        break
        return urls


def _fetch_recipe_title_and_tags(
    session: requests.Session,
    url: str,
    timeout_s: float,
) -> tuple[str, str]:
    """
    Haal titel en een 'tags tekst' uit een receptpagina.

    We pakken:
    - JSON-LD (Recipe) als die er is (keywords/recipeCategory)
    - plus fallback: zichtbare tekst van de pagina

    Return: (title, tags_text)
    """
    # (connect timeout, read timeout)
    resp = session.get(
        url,
        timeout=(6.0, float(timeout_s)),
        headers={"Accept-Encoding": "gzip, deflate"},
    )
    resp.raise_for_status()
    html = resp.text

    soup = BeautifulSoup(html, "html.parser")

    # Title
    title = ""
    h1 = soup.find("h1")
    if h1:
        title = h1.get_text(" ", strip=True)
    if not title and soup.title:
        title = soup.title.get_text(strip=True)
    title = title.strip() or url

    tags_bits: list[str] = []

    # JSON-LD (meestal het meest stabiel)
    for s in soup.find_all("script", attrs={"type": "application/ld+json"}):
        if not s.string:
            continue
        try:
            data = json.loads(s.string)
        except json.JSONDecodeError:
            continue

        # kan dict of list zijn
        items = data if isinstance(data, list) else [data]
        for item in items:
            if not isinstance(item, dict):
                continue
            if item.get("@type") in ("Recipe", ["Recipe"], ["Thing", "Recipe"]):
                kw = item.get("keywords")
                cat = item.get("recipeCategory")
                if isinstance(kw, str):
                    tags_bits.append(kw)
                elif isinstance(kw, list):
                    tags_bits.extend([str(x) for x in kw])
                if isinstance(cat, str):
                    tags_bits.append(cat)
                elif isinstance(cat, list):
                    tags_bits.extend([str(x) for x in cat])

    # Fallback: gebruik ook de breadcrumbs / pagina tekst (grof maar werkt)
    body_text = soup.get_text(" ", strip=True)
    tags_bits.append(body_text)

    tags_text = " | ".join([t for t in tags_bits if t])
    return title, tags_text


def make_session() -> requests.Session:
    """Requests session met retries + nette defaults."""
    session = requests.Session()

    retry = Retry(
        total=4,
        connect=4,
        read=4,
        backoff_factor=0.6,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
        raise_on_status=False,
    )

    adapter = HTTPAdapter(max_retries=retry, pool_connections=20, pool_maxsize=20)
    session.mount("https://", adapter)
    session.mount("http://", adapter)

    session.headers.update(
        {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0 Safari/537.36",
            "Accept-Language": "nl-NL,nl;q=0.9,en;q=0.8",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            # voorkom brotli issues
            "Accept-Encoding": "gzip, deflate",
            "Connection": "close",
        }
    )

    return session


def scrape_recipes(
    sitemap_url: str = SITEMAP_URL,
    want_kooktechniek: str = FILTER_KOOKTECHNIEK,
    want_menugang: str = FILTER_MENUGANG,
    max_sitemap_urls: int = 8000,
    max_attempts: int = 60,
    target_matches: int = 1,
    delay_s: float = 0.6,
    timeout_s: float = 14.0,
    seed: int | None = None,
) -> list[Recipe]:
    """Zoekt random in de sitemap tot we `target_matches` matches hebben (of attempts op zijn)."""

    session = make_session()

    urls = _get_recipe_urls_from_sitemap(
        session=session,
        sitemap_url=sitemap_url,
        max_urls=max_sitemap_urls,
        timeout_s=timeout_s,
    )
    if not urls:
        return []

    want_a = want_kooktechniek.strip().lower()
    want_b = want_menugang.strip().lower()

    matches: dict[tuple[str, str], Recipe] = {}

    # random sample loop
    tried = 0
    if seed is not None:
        rnd = random.Random(seed)
        rnd.shuffle(urls)
    else:
        random.shuffle(urls)

    try:
        for url in urls:
            if tried >= max_attempts:
                break
            if len(matches) >= max(1, int(target_matches)):
                break

            tried += 1

            try:
                title, tags_text = _fetch_recipe_title_and_tags(
                    session=session,
                    url=url,
                    timeout_s=timeout_s,
                )
            except requests.RequestException:
                time.sleep(delay_s)
                continue

            hay = tags_text.lower()
            if want_a in hay and want_b in hay:
                matches[(title, url)] = Recipe(title=title, url=url)

            time.sleep(delay_s)

    except KeyboardInterrupt:
        print("\n⛔️ Afgebroken met Ctrl+C. Ik geef terug wat er al gevonden is...")

    return list(matches.values())


def pick_random_recipe(recipes: Iterable[Recipe]) -> Recipe:
    recipes = list(recipes)
    if not recipes:
        raise RuntimeError(
            "Geen recepten gevonden binnen het aantal pogingen. Verhoog max_attempts of maak de filters ruimer."
        )
    return random.choice(recipes)


# -------------------------
# Streamlit UI
# -------------------------

def _running_in_streamlit() -> bool:
    """Detecteer of dit script door Streamlit gerund wordt."""
    try:
        from streamlit.runtime.scriptrunner import get_script_run_ctx

        return get_script_run_ctx() is not None
    except Exception:
        return False


def run_streamlit_app() -> None:
    import streamlit as st

    st.set_page_config(page_title="Allerhande Random Hoofdgerecht", page_icon="🍽️", layout="centered")

    st.title("🍽️ Random hoofdgerecht generator")
    st.caption("Bron: AH Allerhande (via sitemap). Kies je filters en druk op de dobbelsteen.")

    # Sidebar controls
    with st.sidebar:
        st.header("Instellingen")
        kooktechniek = st.text_input("Kooktechniek (zoekwoord)", value=FILTER_KOOKTECHNIEK)
        menugang = st.text_input("Menugang (zoekwoord)", value=FILTER_MENUGANG)

        st.divider()
        cols = st.columns(2)
        with cols[0]:
            n_suggesties = st.number_input("Aantal suggesties", min_value=1, max_value=10, value=3, step=1)
        with cols[1]:
            max_attempts = st.number_input("Max pogingen", min_value=10, max_value=600, value=120, step=10)

        delay_s = st.slider("Delay per pagina (s)", min_value=0.0, max_value=2.0, value=0.4, step=0.1)
        timeout_s = st.slider("Timeout read (s)", min_value=5.0, max_value=30.0, value=12.0, step=1.0)

        st.divider()
        seed_toggle = st.toggle("Herhaalbare resultaten (seed)", value=False)
        seed = None
        if seed_toggle:
            seed = st.number_input("Seed", min_value=0, max_value=999999, value=42, step=1)

    # Caching sitemap URLs (scheelt veel)
    @st.cache_data(ttl=60 * 60 * 6)  # 6 uur
    def cached_sitemap_urls(max_urls: int, timeout: float) -> list[str]:
        session = make_session()
        return _get_recipe_urls_from_sitemap(
            session=session,
            sitemap_url=SITEMAP_URL,
            max_urls=max_urls,
            timeout_s=timeout,
        )

    # We gebruiken cached urls en checken vervolgens random receptpagina's
    def find_matches(target_matches: int) -> list[Recipe]:
        urls = cached_sitemap_urls(max_urls=8000, timeout=float(timeout_s))
        if not urls:
            return []

        # we hergebruiken scrape_recipes, maar geven de urls via seed/attempts gedrag
        # (scrape_recipes haalt zelf sitemap op; dat is ok, maar caching is sneller)
        # Daarom doen we hier een kleine variant: gewoon scrape_recipes aanroepen met lagere cost.
        return scrape_recipes(
            max_sitemap_urls=8000,
            max_attempts=int(max_attempts),
            target_matches=int(target_matches),
            want_kooktechniek=kooktechniek,
            want_menugang=menugang,
            delay_s=float(delay_s),
            timeout_s=float(timeout_s),
            seed=int(seed) if seed is not None else None,
        )

    c1, c2 = st.columns([1, 2])
    with c1:
        roll = st.button("🎲 Gooi de dobbelsteen", use_container_width=True)
    with c2:
        refresh = st.button("🔁 Nieuwe set (zelfde instellingen)", use_container_width=True)

    if roll or refresh:
        with st.spinner("Even zoeken in Allerhande…"):
            results = find_matches(target_matches=int(n_suggesties))

        if not results:
            st.error(
                "Geen match gevonden binnen het aantal pogingen. "
                "Tip: verhoog ‘Max pogingen’ of maak je zoekwoorden ruimer (bv. ‘kook’ / ‘hoofd’)."
            )
            return

        st.success(f"Gevonden: {len(results)} gerecht(en)")

        for r in results:
            with st.container(border=True):
                st.subheader(r.title)
                st.link_button("Open recept", r.url)

        st.caption("Werkt het traag? Zet delay lager of max pogingen lager. Krijg je weinig matches? Maak filters ruimer.")

    else:
        st.info("Stel je filters in en druk op **🎲 Gooi de dobbelsteen**.")


if __name__ == "__main__":
    # Als je runt via Streamlit: `streamlit run GbotV2.py`
    if _running_in_streamlit():
        run_streamlit_app()
    else:
        recipes = scrape_recipes(
            max_sitemap_urls=8000,
            max_attempts=80,
            target_matches=1,
            delay_s=0.5,
            timeout_s=12.0,
        )
        print(f"Gevonden matchende recepten: {len(recipes)}")

        chosen = pick_random_recipe(recipes)
        print("\n🎲 Random hoofdgerecht (koken):")
        print(f"- {chosen.title}")
        print(f"- {chosen.url}")