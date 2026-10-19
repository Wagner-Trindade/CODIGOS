"""Regressões determinísticas: não consultam nem alteram fontes oficiais."""
import copy
import hashlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

import requests
from scripts import atualizar_normas as coletor


NORMA = {
    "id": "teste", "titulo": "Norma de teste", "urn": "urn:teste",
    "validacao": {
        "minimo_caracteres": 3000, "minimo_artigos": 2,
        "fragmentos_obrigatorios": ["Norma de teste", "1234"],
    },
}
TEXTO = "Norma de teste 1234\nArt. 1 Texto.\nArt. 2 " + "conteudo " * 450
CFG = {
    "fontes": {"normas_portal": "https://normas.leg.br"},
    "seguranca": {"browser_timeout_ms": 60000, "reducao_maxima_percentual": 35},
    "normas": [NORMA],
}


class Relogio:
    def __init__(self):
        self.agora = 0

    def monotonic(self):
        return self.agora

    def sleep(self, segundos):
        self.agora += segundos


def resposta(texto=TEXTO, status=200):
    return SimpleNamespace(
        url="https://normas.leg.br/api/public/binario/teste/texto",
        ok=status < 400, status=status,
        headers={"content-type": "text/plain"}, text=lambda: texto,
    )


class Pagina:
    def __init__(self, relogio, eventos=(), dom=None, erro=None, status=200):
        self.relogio = relogio
        self.eventos = list(eventos)
        self.dom = dom
        self.erro = erro
        self.status = status
        self.fechada = False

    def set_default_timeout(self, timeout):
        pass

    def on(self, evento, callback):
        self.callback = callback

    def goto(self, *args, **kwargs):
        if self.erro:
            raise self.erro
        self.inicio = self.relogio.agora
        return SimpleNamespace(ok=self.status < 400, status=self.status)

    def wait_for_timeout(self, ms):
        self.relogio.sleep(ms / 1000)
        while self.eventos and self.relogio.agora - self.inicio >= self.eventos[0][0]:
            _, response = self.eventos.pop(0)
            self.callback(response)

    def evaluate(self, *args):
        return {"texto": self.dom, "chars": len(self.dom)} if self.dom else None

    def locator(self, selector):
        return SimpleNamespace(inner_text=lambda **kwargs: self.dom or "Menu Ajuda")

    def title(self):
        return "Portal"

    def close(self):
        self.fechada = True


class ColetaTest(unittest.TestCase):
    def setUp(self):
        self.relogio = Relogio()
        self.tempo = patch.object(coletor, "time", self.relogio)
        self.tempo.start()
        self.addCleanup(self.tempo.stop)

    def coletar(self, *paginas, anterior=None):
        self.browser = Mock()
        self.browser.new_page.side_effect = paginas
        return coletor.coletar_texto_normas(self.browser, NORMA, CFG, anterior)

    def test_resposta_tardia_apos_45_segundos(self):
        pagina = Pagina(self.relogio, [(50, resposta())])
        resultado = self.coletar(pagina)
        self.assertEqual(resultado.texto_normalizado, TEXTO.rstrip() + "\n")
        self.assertTrue(pagina.fechada)

    def test_resposta_invalida_nao_encerra_espera(self):
        pagina = Pagina(self.relogio, [(1, resposta("Erro temporario")), (8, resposta())])
        resultado = self.coletar(pagina)
        self.assertEqual(resultado.metodo_coleta, "api-public-binario")
        self.assertEqual(resultado.artigos_detectados, 2)

    def test_resposta_invalida_permite_dom_valido(self):
        pagina = Pagina(self.relogio, [(1, resposta("Erro"))], dom=TEXTO)
        resultado = self.coletar(pagina)
        self.assertEqual(resultado.metodo_coleta, "rendered-dom-container")

    def test_pagina_vazia_reabre_apenas_norma_com_falha(self):
        primeira = Pagina(self.relogio)
        segunda = Pagina(self.relogio, [(1, resposta())])
        resultado = self.coletar(primeira, segunda)
        self.assertEqual(self.browser.new_page.call_count, 2)
        self.assertTrue(primeira.fechada and segunda.fechada)
        self.assertEqual(resultado.sha256, hashlib.sha256((TEXTO.rstrip() + "\n").encode()).hexdigest())

    def test_timeout_de_navegacao_tem_nova_tentativa(self):
        primeira = Pagina(self.relogio, erro=coletor.PlaywrightError("Timeout"))
        segunda = Pagina(self.relogio, [(1, resposta())])
        self.coletar(primeira, segunda)
        self.assertTrue(primeira.fechada and segunda.fechada)

    def test_http_503_na_navegacao_nao_e_aceito(self):
        paginas = [Pagina(self.relogio, dom=TEXTO, status=503) for _ in range(2)]
        with self.assertRaisesRegex(coletor.AtualizacaoError, "HTTP 503"):
            self.coletar(*paginas)
        self.assertTrue(all(p.fechada for p in paginas))

    def test_falha_persistente_para_em_duas_tentativas(self):
        paginas = [Pagina(self.relogio) for _ in range(2)]
        with self.assertRaises(coletor.AtualizacaoError):
            self.coletar(*paginas)
        self.assertEqual(self.browser.new_page.call_count, 2)
        self.assertTrue(all(p.fechada for p in paginas))

    def test_sucesso_nao_repete_coleta(self):
        self.coletar(Pagina(self.relogio, [(1, resposta())]))
        self.assertEqual(self.browser.new_page.call_count, 1)

    def test_reducao_anormal_continua_bloqueada(self):
        paginas = [Pagina(self.relogio, [(1, resposta())], dom=TEXTO) for _ in range(2)]
        with self.assertRaisesRegex(coletor.AtualizacaoError, "menor"):
            self.coletar(*paginas, anterior=TEXTO * 3)

    def test_tamanho_fragmentos_e_artigos_continuam_obrigatorios(self):
        invalidos = ["curto", TEXTO.replace("1234", "xxxx"), TEXTO.replace("Art.", "Item")]
        for texto in invalidos:
            with self.subTest(texto=texto[:40]), self.assertRaises(coletor.AtualizacaoError):
                coletor.validar_texto(NORMA, texto, None, 35)

    def test_falha_de_uma_norma_preserva_todos_os_arquivos(self):
        cfg = copy.deepcopy(CFG)
        outra = copy.deepcopy(NORMA)
        outra.update(id="outra", titulo="Outra norma")
        cfg["normas"].append(outra)
        valido = coletor.TextoColetado(TEXTO, TEXTO.rstrip() + "\n", "https://normas.leg.br/", "text/plain", "teste", "hash-novo", 2)
        with tempfile.TemporaryDirectory() as pasta:
            raiz = Path(pasta)
            for norma in cfg["normas"]:
                for nome in ["texto.txt", "fonte.txt", "sha256.txt", "metadata.json", "metadata-senado.json"]:
                    path = raiz / norma["id"] / nome
                    path.parent.mkdir(exist_ok=True)
                    path.write_text("{}" if nome.endswith("json") else "original", encoding="utf-8")
            antes = {p.relative_to(raiz): p.read_bytes() for p in raiz.rglob("*") if p.is_file()}
            playwright = MagicMock()
            with (
                patch.object(coletor, "OUTPUT_ROOT", raiz),
                patch.object(coletor, "carregar_config", return_value=cfg),
                patch.object(coletor, "criar_sessao"),
                patch.object(coletor, "preflight_senado"),
                patch.object(coletor, "coletar_metadata_senado", return_value={"urn": "teste"}),
                patch.object(coletor, "sync_playwright", return_value=playwright),
                patch.object(coletor, "coletar_texto_normas", side_effect=[valido, coletor.AtualizacaoError("fonte indisponivel")]),
                patch.object(coletor, "aplicar_resultados", wraps=coletor.aplicar_resultados) as aplicar,
            ):
                self.assertEqual(coletor.executar(Path("nao-lido.yml")), 2)
                aplicar.assert_not_called()
            depois = {p.relative_to(raiz): p.read_bytes() for p in raiz.rglob("*") if p.is_file()}
            self.assertEqual(antes, depois)

    def test_api_senado_503_e_timeout_tem_repeticao(self):
        for erro in [requests.Timeout("timeout"), requests.HTTPError("HTTP 503")]:
            with self.subTest(erro=type(erro).__name__):
                session = Mock()
                ok = Mock(status_code=200)
                ok.json.return_value = {"urn": "teste"}
                session.get.side_effect = [erro, ok]
                self.assertEqual(coletor.requisicao_json_com_retry(session, "https://legis.senado.leg.br/", {}, 30, 2, 2), {"urn": "teste"})
                self.assertEqual(session.get.call_count, 2)


if __name__ == "__main__":
    unittest.main()
