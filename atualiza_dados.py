import io
import json
import re
import sys
import time
import unicodedata
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import urljoin

import pandas as pd
import requests
from bs4 import BeautifulSoup
from pypdf import PdfReader
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

BASE_SISCAM = "https://santabarbara.siscam.com.br"
BASE_CONSULTA = "https://consulta.siscam.com.br/camarasantabarbara"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
}

APELIDOS_VEREADORES = {
    "Alex Dantas": ["alex dantas", "alexandro dantas"],
    "Arnaldo Alves": ["arnaldo alves"],
    "Cabo Dorigon": ["cabo dorigon", "dorigon"],
    "Careca do Esporte": ["careca do esporte", "valmir alcantara"],
    "Carlos Fontes": ["carlos fontes", "carlos alberto portella fontes"],
    "Celso Ávila": ["celso avila", "celso luis de avila"],
    "Esther Moraes": ["esther moraes", "esther galina"],
    "Felipe Corá": ["felipe cora"],
    "Gustavo Bagnoli": ["gustavo bagnoli"],
    "Isac Sorrillo": ["isac sorrillo", "isac motorista"],
    "Joi Fornasari": ["joi fornasari", "jose luis fornasari"],
    "Juca Bortolucci": ["juca bortolucci"],
    "Kifú": ["kifu", "julio cesar kifu"],
    "Lúcio Donizete": ["lucio donizete"],
    "Marcelo Cury": ["marcelo cury"],
    "Paulo Monaro": ["paulo monaro"],
    "Rony Tavares": ["rony tavares"],
    "Tikinho TK": ["tikinho tk", "tikinho"],
    "Wilson da Engenharia": ["wilson da engenharia"],
}


def criar_sessao_resiliente() -> requests.Session:
    """Cria sessão HTTP com retentativas automáticas em caso de lentidão ou timeout do Siscam."""
    session = requests.Session()
    retry = Retry(
        total=4,
        connect=4,
        read=4,
        backoff_factor=2.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def get_com_retry(session: requests.Session, url: str, timeout: int = 90) -> requests.Response:
    """Faz GET com retentativa manual extra caso ocorra ReadTimeout."""
    ultimo_erro = None
    for tentativa in range(1, 5):
        try:
            resp = session.get(url, headers=HEADERS, timeout=timeout)
            resp.raise_for_status()
            return resp
        except Exception as e:
            ultimo_erro = e
            espera = tentativa * 5
            print(f"   [Tentativa {tentativa}/4] Servidor lento em {url} ({type(e).__name__}). Aguardando {espera}s...")
            time.sleep(espera)
    raise ultimo_erro


def normalizar(texto: str) -> str:
    nfkd = unicodedata.normalize("NFKD", texto or "")
    return "".join(c for c in nfkd if not unicodedata.combining(c)).lower().strip()


def identificar_vereadores(texto_autoria: str) -> list:
    norm = normalizar(texto_autoria)
    encontrados = []
    for nome_oficial, aliases in APELIDOS_VEREADORES.items():
        if any(alias in norm for alias in aliases):
            encontrados.append(nome_oficial)
    return encontrados if encontrados else [texto_autoria.strip()]


def classificar_proposta(tipo: str, ementa: str) -> str:
    t = normalizar(tipo)
    e = normalizar(ementa)
    if "indicacao" in t:
        return "bairros"
    if "requerimento" in t:
        return "cobrancas"
    if "mocao" in t:
        if any(k in e for k in ["aplauso", "parabens", "louvor", "pesar", "congratul", "homenagem"]):
            return "homenagens"
        return "outros"
    if "decreto legislativo" in t:
        return "homenagens"
    if "projeto de lei" in t or "emenda a lei organica" in t:
        if any(k in e for k in ["denominacao", "denomina", "nome de", "calendario oficial", "dia municipal", "semana municipal"]):
            return "homenagens"
        return "leis"
    return "outros"


def coletar_todas_proposituras(session: requests.Session, ano: int = 2026, max_paginas: int = 150) -> list:
    propostas = []
    ids_vistos = set()

    for pagina in range(1, max_paginas + 1):
        url = (
            f"{BASE_SISCAM}/Documentos/Pesquisa"
            f"?Pesquisa=Avancada&id=74&pagina={pagina}&Modulo=8&Ano={ano}"
        )
        print(f"   Lendo página {pagina} de proposituras ({ano})...")
        resp = get_com_retry(session, url, timeout=90)

        soup = BeautifulSoup(resp.text, "html.parser")
        links_doc = soup.select("h4 a[href*='/Documentos/Documento/']")
        if not links_doc:
            print(f"   Fim das proposituras na página {pagina}.")
            break

        novos_na_pagina = 0
        for a in links_doc:
            href = a.get("href", "")
            if href in ids_vistos:
                continue
            ids_vistos.add(href)
            novos_na_pagina += 1

            titulo = a.get_text(" ", strip=True)
            bloco = a.find_parent("div")
            campos = {"tipo": "", "data": "", "autoria": "", "assunto": "", "situacao": "TRAMITANDO"}

            if bloco:
                for p in bloco.find_all("p"):
                    txt = p.get_text(" ", strip=True)
                    low = normalizar(txt)
                    if low.startswith("tipo:"):
                        campos["tipo"] = txt.split(":", 1)[1].strip()
                    elif low.startswith("data:"):
                        campos["data"] = txt.split(":", 1)[1].strip()
                    elif low.startswith("autoria:"):
                        campos["autoria"] = txt.split(":", 1)[1].strip()
                    elif low.startswith("assunto:") or low.startswith("ementa:"):
                        campos["assunto"] = txt.split(":", 1)[1].strip()
                    elif low.startswith("situacao:"):
                        campos["situacao"] = txt.split(":", 1)[1].strip()

            if str(ano) not in (campos["data"] + titulo):
                continue

            vereadores = identificar_vereadores(campos["autoria"])
            for ver in vereadores:
                propostas.append({
                    "vereador": ver,
                    "autoria_original": campos["autoria"],
                    "titulo": titulo,
                    "tipo": campos["tipo"] or titulo.split("Nº")[0].strip(),
                    "data": campos["data"],
                    "ementa": campos["assunto"],
                    "situacao": campos["situacao"],
                    "grupo": classificar_proposta(campos["tipo"] or titulo, campos["assunto"]),
                    "fonte_url": urljoin(BASE_SISCAM, href),
                })

        if novos_na_pagina == 0:
            break
        time.sleep(0.8)

    return propostas


def extrair_texto_arquivo(session: requests.Session, url_arquivo: str) -> tuple[str, str]:
    resp = get_com_retry(session, url_arquivo, timeout=90)
    conteudo = resp.content or b""

    if conteudo.startswith(b"PK\x03\x04"):
        try:
            with zipfile.ZipFile(io.BytesIO(conteudo)) as zf:
                if "word/document.xml" in zf.namelist():
                    xml_bytes = zf.read("word/document.xml")
                    root = ET.fromstring(xml_bytes)
                    paragrafos = []
                    for p in root.iter():
                        if p.tag.endswith("}p"):
                            texto_p = "".join(t.text for t in p.iter() if t.tag.endswith("}t") and t.text)
                            if texto_p.strip():
                                paragrafos.append(texto_p.strip())
                    return "\n".join(paragrafos), "docx"
        except Exception as e:
            return f"[Erro ao ler DOCX: {e}]", "docx_erro"

    if conteudo.lstrip().startswith(b"%PDF"):
        try:
            reader = PdfReader(io.BytesIO(conteudo))
            texto = "\n".join(page.extract_text() or "" for page in reader.pages)
            return texto, "pdf"
        except Exception as e:
            return f"[Erro ao ler PDF: {e}]", "pdf_erro"

    soup = BeautifulSoup(conteudo, "html.parser")
    return soup.get_text(" ", strip=True), "html"


def coletar_sessoes_e_atas(session: requests.Session, ano: int = 2026) -> list:
    sessoes_extraidas = []
    urls_sessoes = []

    for pagina in range(1, 12):
        url_lista = f"{BASE_CONSULTA}/sessoes?pagina={pagina}" if pagina > 1 else f"{BASE_CONSULTA}/sessoes"
        resp = get_com_retry(session, url_lista, timeout=90)
        soup = BeautifulSoup(resp.text, "html.parser")
        linhas = soup.select("table tbody tr")
        if not linhas:
            break

        achou_ano = False
        for tr in linhas:
            cols = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
            link_tag = tr.find("a", href=re.compile(r"/Sessoes/Sessao/\d+"))
            if not link_tag:
                continue
            texto_linha = " ".join(cols)
            if str(ano) in texto_linha:
                achou_ano = True
                url_abs = urljoin(BASE_CONSULTA + "/", link_tag["href"])
                if not any(u["url_sessao"] == url_abs for u in urls_sessoes):
                    urls_sessoes.append({
                        "sessao": cols[0],
                        "data": cols[1] if len(cols) > 1 else "",
                        "url_sessao": url_abs,
                    })
        if not achou_ano:
            break
        time.sleep(0.8)

    print(f"   Encontradas {len(urls_sessoes)} sessões de {ano}. Lendo atas...")

    for item in urls_sessoes:
        s_resp = get_com_retry(session, item["url_sessao"], timeout=90)
        s_soup = BeautifulSoup(s_resp.text, "html.parser")

        ata_link = None
        for a in s_soup.find_all("a", href=re.compile(r"/arquivo\?Id=\d+")):
            rotulo = (a.get_text(" ", strip=True) + " " + (a.get("title") or "")).lower()
            if "ata" in rotulo:
                ata_link = urljoin(item["url_sessao"], a["href"])
                break

        if not ata_link:
            primeiro_arq = s_soup.find("a", href=re.compile(r"/arquivo\?Id=\d+"))
            if primeiro_arq:
                ata_link = urljoin(item["url_sessao"], primeiro_arq["href"])

        registro = {
            "sessao": item["sessao"],
            "data": item["data"],
            "url_sessao": item["url_sessao"],
            "url_ata": ata_link,
            "formato_ata": None,
            "presentes": [],
            "votacoes_nominais": [],
        }

        if ata_link:
            texto_ata, formato = extrair_texto_arquivo(session, ata_link)
            registro["formato_ata"] = formato
            texto_limpo = re.sub(r"\s+", " ", texto_ata)

            m_pres = re.search(
                r"PRESENTES os seguintes vereadores[:\s]+(.*?)(?:\.|Havendo n[úu]mero legal)",
                texto_limpo,
                re.I,
            )
            if m_pres:
                bloco_pres = m_pres.group(1)
                registro["presentes"] = identificar_vereadores(bloco_pres)

            for m_voto in re.finditer(
                r"(\d+)\s*\([^)]+\)\s*votos?\s+favor[áa]veis(?:,\s*(\d+)\s*\([^)]+\)\s*votos?\s+contr[áa]rios:\s*([^\.]+))?(?:.*?aus[êe]ncias?:\s*([^\.]+))?",
                texto_limpo,
                re.I,
            ):
                registro["votacoes_nominais"].append({
                    "favoraveis": int(m_voto.group(1)),
                    "contrarios": int(m_voto.group(2)) if m_voto.group(2) else 0,
                    "contrarios_lista": identificar_vereadores(m_voto.group(3)) if m_voto.group(3) else [],
                    "ausentes_lista": identificar_vereadores(m_voto.group(4)) if m_voto.group(4) else [],
                })

            print(f"   OK: {item['sessao']} ({formato}) -> {len(registro['presentes'])} presentes")
        else:
            print(f"   Sem ata anexada ainda: {item['sessao']}")

        sessoes_extraidas.append(registro)
        time.sleep(0.8)

    return sessoes_extraidas


def validar_coleta(novo: dict, caminho_atual: Path) -> tuple[bool, str]:
    """Impede publicar dados vazios, corrompidos ou menores que a última versão boa."""
    propostas = novo.get("propostas", [])
    sessoes = novo.get("sessoes", [])

    if len(propostas) < 50:
        return False, f"Poucas propostas coletadas ({len(propostas)} < 50)."

    if len(sessoes) < 10:
        return False, f"Poucas sessões coletadas ({len(sessoes)} < 10)."

    vereadores_encontrados = {
        p.get("vereador") for p in propostas if p.get("vereador") in APELIDOS_VEREADORES
    }
    if len(vereadores_encontrados) < 15:
        return False, f"Apenas {len(vereadores_encontrados)} de 19 vereadores identificados nas propostas."

    atas_validas = [s for s in sessoes if len(s.get("presentes", [])) >= 10]
    if len(atas_validas) < 5:
        return False, f"Poucas atas com lista de presença válida ({len(atas_validas)} < 5)."

    if caminho_atual.exists():
        try:
            anterior = json.loads(caminho_atual.read_text(encoding="utf-8"))
            ant_props = len(anterior.get("propostas", []))
            ant_sess = len(anterior.get("sessoes", []))

            if ant_props > 0 and len(propostas) < int(ant_props * 0.90):
                return False, (
                    f"Queda suspeita nas propostas: antes havia {ant_props}, agora vieram {len(propostas)}."
                )

            if ant_sess > 0 and len(sessoes) < ant_sess:
                return False, (
                    f"Queda suspeita nas sessões: antes havia {ant_sess}, agora vieram {len(sessoes)}."
                )
        except Exception as e:
            print(f"   [Aviso] Não foi possível ler versão anterior para comparação: {e}")

    return True, "Coleta validada com sucesso."


def main():
    pasta_script = Path(__file__).resolve().parent
    caminho_saida = pasta_script / "dados_camara.json"
    caminho_temp = pasta_script / "dados_camara_temp.json"

    session = criar_sessao_resiliente()

    try:
        print("1. Coletando 100% das proposituras de 2026 no Siscam...")
        propostas = coletar_todas_proposituras(session, ano=2026)

        print("2. Coletando sessões e atas de 2026 (DOCX e PDF)...")
        sessoes = coletar_sessoes_e_atas(session, ano=2026)
    except Exception as e:
        print(f"\n[BLOQUEADO] Erro durante a coleta na Câmara: {e}")
        print("Mantendo a última versão boa de dados_camara.json.")
        sys.exit(1)

    saida = {
        "gerado_em": pd.Timestamp.now(tz="America/Sao_Paulo").isoformat(),
        "fonte_oficial": BASE_SISCAM,
        "total_propostas": len(propostas),
        "total_sessoes": len(sessoes),
        "propostas": propostas,
        "sessoes": sessoes,
    }

    ok, motivo = validar_coleta(saida, caminho_saida)
    if not ok:
        print(f"\n[BLOQUEADO] {motivo}")
        print("O site NÃO será republicado; a última versão boa foi preservada.")
        sys.exit(1)

    caminho_temp.write_text(json.dumps(saida, ensure_ascii=False, indent=2), encoding="utf-8")
    caminho_temp.replace(caminho_saida)

    print(f"\n[OK] {motivo}")
    print(f"Salvo em: {caminho_saida} ({len(propostas)} propostas | {len(sessoes)} sessões)")


if __name__ == "__main__":
    main()