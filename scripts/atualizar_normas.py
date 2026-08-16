#!/usr/bin/env python3
"""
Atualizador versionado de normas jurídicas brasileiras.

Fluxo:
1. lê config/normas.yml;
2. valida a URN na API oficial do Senado;
3. abre o portal oficial normas.leg.br com um navegador headless;
4. captura a resposta pública de texto utilizada pela própria aplicação;
5. normaliza e valida o conteúdo;
6. compara SHA-256 com a versão local;
7. grava somente normas efetivamente alteradas;
8. nunca substitui arquivos válidos quando uma coleta/validação falha.

O Git é responsável pelo histórico das versões anteriores.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import sys
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import requests
import yaml
from bs4 import BeautifulSoup
from playwright.sync_api import Browser, Page, Response, sync_playwright


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "config" / "normas.yml"
OUTPUT_ROOT = ROOT / "normas"


class AtualizacaoError(RuntimeError):
    pass


@dataclass
class TextoColetado:
    texto_bruto: str
    texto_normalizado: str
    url_texto: str
    content_type: str
    sha256: str
    artigos_detectados: int


@dataclass
class ResultadoNorma:
    config: dict[str, Any]
    senate_metadata: Any
    texto: TextoColetado
    mudou: bool
    hash_anterior: str | None


def eprint(*args: Any) -> None:
    print(*args, file=sys.stderr)


def carregar_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise AtualizacaoError(f"Arquivo de configuração não encontrado: {path}")

    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise AtualizacaoError("Configuração YAML inválida.")

    normas = data.get("normas")
    if not isinstance(normas, list) or not normas:
        raise AtualizacaoError("A configuração não contém nenhuma norma.")

    ids: set[str] = set()
    urns: set[str] = set()

    for item in normas:
        if not isinstance(item, dict):
            raise AtualizacaoError("Cada item de 'normas' deve ser um objeto YAML.")

        for campo in ("id", "titulo", "urn", "validacao"):
            if campo not in item:
                raise AtualizacaoError(
                    f"Norma sem campo obrigatório '{campo}': {item!r}"
                )

        norma_id = str(item["id"]).strip()
        urn = str(item["urn"]).strip()

        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", norma_id):
            raise AtualizacaoError(
                f"id inválido '{norma_id}'. Use apenas minúsculas, números e hífens."
            )
        if norma_id in ids:
            raise AtualizacaoError(f"id duplicado: {norma_id}")
        if urn in urns:
            raise AtualizacaoError(f"URN duplicada: {urn}")

        ids.add(norma_id)
        urns.add(urn)

    return data


def criar_sessao() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "Accept": "application/json",
            "User-Agent": (
                "AtualizadorLegislacaoGitHub/1.0 "
                "(consulta automatizada de dados publicos oficiais)"
            ),
        }
    )
    return session


def requisicao_json_com_retry(
    session: requests.Session,
    url: str,
    params: dict[str, Any],
    timeout: int,
    tentativas: int,
    espera: float,
) -> Any:
    ultimo_erro: Exception | None = None

    for tentativa in range(1, tentativas + 1):
        try:
            resp = session.get(url, params=params, timeout=timeout)

            if resp.status_code == 429:
                retry_after = resp.headers.get("Retry-After")
                atraso = float(retry_after) if retry_after else espera * tentativa
                eprint(
                    f"API retornou 429; nova tentativa em {atraso:.1f}s "
                    f"({tentativa}/{tentativas})."
                )
                time.sleep(atraso)
                continue

            if 500 <= resp.status_code <= 599:
                raise requests.HTTPError(
                    f"HTTP {resp.status_code} em {resp.url}", response=resp
                )

            resp.raise_for_status()

            try:
                return resp.json()
            except ValueError as exc:
                inicio = resp.text[:300].replace("\n", " ")
                raise AtualizacaoError(
                    f"A fonte não devolveu JSON válido. Início da resposta: {inicio!r}"
                ) from exc

        except (requests.RequestException, AtualizacaoError) as exc:
            ultimo_erro = exc
            if tentativa == tentativas:
                break
            atraso = espera * tentativa
            eprint(
                f"Falha HTTP: {exc}. Nova tentativa em {atraso:.1f}s "
                f"({tentativa}/{tentativas})."
            )
            time.sleep(atraso)

    raise AtualizacaoError(
        f"Não foi possível consultar a fonte após {tentativas} tentativas: {ultimo_erro}"
    )


def coletar_metadata_senado(
    session: requests.Session,
    norma: dict[str, Any],
    cfg: dict[str, Any],
) -> Any:
    fontes = cfg["fontes"]
    seguranca = cfg["seguranca"]

    metadata = requisicao_json_com_retry(
        session=session,
        url=str(fontes["senado_urn"]),
        params={"urn": norma["urn"], "v": 3},
        timeout=int(seguranca["http_timeout_segundos"]),
        tentativas=int(seguranca["tentativas_http"]),
        espera=float(seguranca["espera_entre_tentativas_segundos"]),
    )

    # Validação propositalmente tolerante à estrutura JSON:
    # a API pode evoluir sem que uma simples mudança de nomes quebre o robô.
    serializado = json.dumps(metadata, ensure_ascii=False, sort_keys=True)
    if not serializado or len(serializado) < 20:
        raise AtualizacaoError(
            f"{norma['titulo']}: metadados do Senado vazios ou anormalmente curtos."
        )

    return metadata


def normalizar_para_comparacao(texto: str) -> str:
    """
    Normaliza apenas representação técnica:
    - converte HTML em texto quando houver marcação;
    - decodifica entidades;
    - normaliza Unicode e finais de linha;
    - elimina espaços finais;
    - limita sequências de linhas vazias.

    Não resume nem remove dispositivos jurídicos.
    """
    bruto = texto.replace("\x00", "")
    parece_html = bool(
        re.search(
            r"<(?:html|body|div|p|span|article|section|br|table|h[1-6])\b",
            bruto,
            flags=re.I,
        )
    )

    if parece_html:
        soup = BeautifulSoup(bruto, "html.parser")
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        bruto = soup.get_text("\n")

    bruto = html.unescape(bruto)
    bruto = unicodedata.normalize("NFC", bruto)
    bruto = bruto.replace("\r\n", "\n").replace("\r", "\n")
    bruto = bruto.replace("\u00a0", " ")

    linhas = [linha.rstrip() for linha in bruto.split("\n")]
    texto_normalizado = "\n".join(linhas).strip() + "\n"
    texto_normalizado = re.sub(r"\n{4,}", "\n\n\n", texto_normalizado)

    return texto_normalizado


def texto_sem_acentos(texto: str) -> str:
    decomposto = unicodedata.normalize("NFD", texto)
    return "".join(c for c in decomposto if unicodedata.category(c) != "Mn")


def contar_artigos(texto: str) -> int:
    # Conta ocorrências iniciadas por "Art." ou "Artº" em linha.
    padrao = re.compile(
        r"(?im)^\s*Art(?:igo)?\.?\s*º?\s*\d+[A-Za-zº°\-]*"
    )
    return len(padrao.findall(texto))


def validar_texto(
    norma: dict[str, Any],
    texto: str,
    texto_anterior: str | None,
    reducao_maxima_percentual: float,
) -> int:
    validacao = norma["validacao"]
    minimo_caracteres = int(validacao.get("minimo_caracteres", 5000))
    minimo_artigos = int(validacao.get("minimo_artigos", 1))
    fragmentos = list(validacao.get("fragmentos_obrigatorios", []))

    if len(texto) < minimo_caracteres:
        raise AtualizacaoError(
            f"{norma['titulo']}: texto com {len(texto):,} caracteres; "
            f"mínimo configurado = {minimo_caracteres:,}."
        )

    comparavel = texto_sem_acentos(texto).casefold()
    for fragmento in fragmentos:
        esperado = texto_sem_acentos(str(fragmento)).casefold()
        if esperado not in comparavel:
            raise AtualizacaoError(
                f"{norma['titulo']}: fragmento obrigatório não encontrado: "
                f"{fragmento!r}."
            )

    artigos = contar_artigos(texto)
    if artigos < minimo_artigos:
        raise AtualizacaoError(
            f"{norma['titulo']}: apenas {artigos} artigos detectados; "
            f"mínimo configurado = {minimo_artigos}."
        )

    if texto_anterior:
        limite = 1.0 - (reducao_maxima_percentual / 100.0)
        if len(texto) < len(texto_anterior) * limite:
            reducao = 100.0 * (1 - len(texto) / len(texto_anterior))
            raise AtualizacaoError(
                f"{norma['titulo']}: o novo texto ficou {reducao:.1f}% menor "
                "que a versão versionada. A atualização foi bloqueada por segurança."
            )

    return artigos


def coletar_texto_normas(
    browser: Browser,
    norma: dict[str, Any],
    cfg: dict[str, Any],
    texto_anterior: str | None,
) -> TextoColetado:
    portal = str(cfg["fontes"]["normas_portal"]).rstrip("/") + "/"
    timeout_ms = int(cfg["seguranca"]["browser_timeout_ms"])
    reducao_max = float(cfg["seguranca"]["reducao_maxima_percentual"])

    url_pagina = portal + "?" + urlencode({"urn": norma["urn"]})

    page: Page = browser.new_page()
    page.set_default_timeout(timeout_ms)

    candidatos: list[tuple[int, str, str, str]] = []

    def capturar(response: Response) -> None:
        url = response.url
        if "/api/public/binario/" not in url or not url.rstrip("/").endswith("/texto"):
            return
        if not response.ok:
            return

        try:
            corpo = response.text()
        except Exception:
            return

        if not corpo:
            return

        content_type = response.headers.get("content-type", "")
        candidatos.append((len(corpo), url, corpo, content_type))

    page.on("response", capturar)

    try:
        page.goto(url_pagina, wait_until="domcontentloaded", timeout=timeout_ms)

        # A SPA pode carregar o texto alguns segundos depois do DOM.
        prazo = time.monotonic() + min(timeout_ms / 1000, 45)
        ultima_contagem = 0
        estavel_desde = time.monotonic()

        while time.monotonic() < prazo:
            page.wait_for_timeout(500)

            if len(candidatos) != ultima_contagem:
                ultima_contagem = len(candidatos)
                estavel_desde = time.monotonic()

            # Após aparecer ao menos um texto, espera 1,5 s por eventual
            # candidato maior (p. ex., seleção final da versão vigente).
            if candidatos and (time.monotonic() - estavel_desde) >= 1.5:
                break

        if not candidatos:
            titulo = page.title()
            body_preview = ""
            try:
                body_preview = page.locator("body").inner_text(timeout=3000)[:500]
            except Exception:
                pass
            raise AtualizacaoError(
                f"{norma['titulo']}: o portal não carregou nenhuma resposta "
                "'/api/public/binario/.../texto'. "
                f"Título da página: {titulo!r}. Prévia: {body_preview!r}"
            )

        # A versão legislativa integral tende a ser a maior resposta textual.
        _, source_url, bruto, content_type = max(candidatos, key=lambda item: item[0])

        normalizado = normalizar_para_comparacao(bruto)
        artigos = validar_texto(
            norma=norma,
            texto=normalizado,
            texto_anterior=texto_anterior,
            reducao_maxima_percentual=reducao_max,
        )

        digest = hashlib.sha256(normalizado.encode("utf-8")).hexdigest()

        return TextoColetado(
            texto_bruto=bruto,
            texto_normalizado=normalizado,
            url_texto=source_url,
            content_type=content_type,
            sha256=digest,
            artigos_detectados=artigos,
        )

    finally:
        page.close()


def ler_texto_anterior(norma_id: str) -> str | None:
    path = OUTPUT_ROOT / norma_id / "texto.txt"
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8")


def ler_hash_anterior(norma_id: str) -> str | None:
    path = OUTPUT_ROOT / norma_id / "sha256.txt"
    if not path.exists():
        return None
    value = path.read_text(encoding="utf-8").strip()
    return value or None


def gravar_atomico(path: Path, conteudo: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(conteudo, encoding="utf-8", newline="\n")
    tmp.replace(path)


def metadata_estavel(
    resultado: ResultadoNorma,
    cfg: dict[str, Any],
    timestamp: str,
) -> dict[str, Any]:
    norma = resultado.config
    texto = resultado.texto

    return {
        "id": norma["id"],
        "titulo": norma["titulo"],
        "urn": norma["urn"],
        "referencia": norma.get("referencia", {}),
        "sha256_texto_normalizado": texto.sha256,
        "caracteres_texto_normalizado": len(texto.texto_normalizado),
        "artigos_detectados": texto.artigos_detectados,
        "alteracao_detectada_em": timestamp,
        "fontes": {
            "portal_normas": (
                str(cfg["fontes"]["normas_portal"]).rstrip("/")
                + "/?"
                + urlencode({"urn": norma["urn"]})
            ),
            "texto_publico_observado": texto.url_texto,
            "api_metadados_senado": str(cfg["fontes"]["senado_urn"]),
        },
        "nota": (
            "O campo 'alteracao_detectada_em' só muda quando o SHA-256 do texto "
            "normalizado muda. A execução diária sem alteração não gera commit."
        ),
    }


def aplicar_resultados(
    resultados: list[ResultadoNorma],
    cfg: dict[str, Any],
    timezone: str,
    dry_run: bool,
) -> list[str]:
    alteradas = [r for r in resultados if r.mudou]

    if not alteradas:
        print("Nenhuma alteração textual detectada.")
        return []

    ids = [r.config["id"] for r in alteradas]
    if dry_run:
        print("DRY-RUN: alterações detectadas, sem gravação:", ", ".join(ids))
        return ids

    timestamp = datetime.now(ZoneInfo(timezone)).isoformat(timespec="seconds")

    # Só chegamos aqui depois que TODAS as normas foram coletadas e validadas.
    for resultado in alteradas:
        norma_id = resultado.config["id"]
        pasta = OUTPUT_ROOT / norma_id
        pasta.mkdir(parents=True, exist_ok=True)

        gravar_atomico(pasta / "texto.txt", resultado.texto.texto_normalizado)
        gravar_atomico(pasta / "fonte.txt", resultado.texto.texto_bruto)
        gravar_atomico(pasta / "sha256.txt", resultado.texto.sha256 + "\n")

        meta = metadata_estavel(resultado, cfg, timestamp)
        gravar_atomico(
            pasta / "metadata.json",
            json.dumps(meta, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )

        # Snapshot dos metadados estruturados do Senado no instante em que
        # a mudança textual foi detectada. Não é usado para provocar commits.
        gravar_atomico(
            pasta / "metadata-senado.json",
            json.dumps(
                resultado.senate_metadata,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
        )

    # Manifesto geral: só é regravado quando houve alteração em alguma norma.
    manifesto: dict[str, Any] = {
        "gerado_em": timestamp,
        "timezone": timezone,
        "normas": [],
    }

    for norma in cfg["normas"]:
        meta_path = OUTPUT_ROOT / norma["id"] / "metadata.json"
        if meta_path.exists():
            try:
                manifesto["normas"].append(
                    json.loads(meta_path.read_text(encoding="utf-8"))
                )
            except json.JSONDecodeError as exc:
                raise AtualizacaoError(
                    f"metadata.json local inválido em {meta_path}: {exc}"
                ) from exc

    gravar_atomico(
        OUTPUT_ROOT / "manifesto.json",
        json.dumps(manifesto, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )

    return ids


def executar(config_path: Path, dry_run: bool = False) -> int:
    cfg = carregar_config(config_path)
    timezone = str(cfg.get("projeto", {}).get("timezone", "America/Sao_Paulo"))

    session = criar_sessao()
    resultados: list[ResultadoNorma] = []
    erros: list[str] = []

    print(f"Configuração: {config_path}")
    print(f"Normas cadastradas: {len(cfg['normas'])}")

    # Uma única instância do Chromium atende todas as normas.
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            for i, norma in enumerate(cfg["normas"], start=1):
                titulo = norma["titulo"]
                norma_id = norma["id"]
                print(f"[{i}/{len(cfg['normas'])}] {titulo}")

                try:
                    anterior = ler_texto_anterior(norma_id)
                    hash_anterior = ler_hash_anterior(norma_id)

                    # Consulta estruturada ao Senado.
                    senate_metadata = coletar_metadata_senado(
                        session=session,
                        norma=norma,
                        cfg=cfg,
                    )

                    # Mantém folga enorme em relação ao limite público da API.
                    time.sleep(0.35)

                    texto = coletar_texto_normas(
                        browser=browser,
                        norma=norma,
                        cfg=cfg,
                        texto_anterior=anterior,
                    )

                    mudou = texto.sha256 != hash_anterior
                    estado = "ALTERADA" if mudou else "sem alteração"
                    print(
                        f"  {estado} | SHA-256 {texto.sha256[:12]}… | "
                        f"{len(texto.texto_normalizado):,} caracteres | "
                        f"{texto.artigos_detectados} artigos detectados"
                    )

                    resultados.append(
                        ResultadoNorma(
                            config=norma,
                            senate_metadata=senate_metadata,
                            texto=texto,
                            mudou=mudou,
                            hash_anterior=hash_anterior,
                        )
                    )

                except Exception as exc:
                    mensagem = f"{titulo}: {exc}"
                    erros.append(mensagem)
                    eprint("ERRO:", mensagem)

        finally:
            browser.close()
            session.close()

    # Política transacional: se UMA norma falhou, NENHUMA é gravada.
    if erros:
        eprint("\nAtualização CANCELADA. Arquivos existentes foram preservados.")
        for erro in erros:
            eprint(" -", erro)
        return 2

    alteradas = aplicar_resultados(
        resultados=resultados,
        cfg=cfg,
        timezone=timezone,
        dry_run=dry_run,
    )

    if alteradas:
        print("Normas gravadas:", ", ".join(alteradas))
    print("Concluído com segurança.")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Atualiza textos oficiais de normas jurídicas versionadas no Git."
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG,
        help=f"Arquivo YAML (padrão: {DEFAULT_CONFIG})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Coleta e valida tudo, mas não grava arquivos.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        return executar(args.config.resolve(), dry_run=args.dry_run)
    except KeyboardInterrupt:
        eprint("Interrompido pelo usuário.")
        return 130
    except Exception as exc:
        eprint(f"FALHA FATAL: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
