import hashlib
import json
import unittest
from pathlib import Path

from bs4 import BeautifulSoup

from scripts.atualizar_normas import (
    TextoColetado,
    normalizar_para_comparacao,
    texto_mudou,
)


ROOT = Path(__file__).resolve().parents[1]
CDC = ROOT / "normas" / "codigo-defesa-consumidor"


class NormalizarNormasTest(unittest.TestCase):
    def test_novo_normalizador_nao_simula_alteracao_legal(self):
        pasta = ROOT / "normas" / "codigo-penal"
        fonte = (pasta / "fonte.txt").read_text(encoding="utf-8")
        novo = normalizar_para_comparacao(fonte)
        hash_antigo = (pasta / "sha256.txt").read_text(encoding="utf-8").strip()
        self.assertNotEqual(hash_antigo, hashlib.sha256(novo.encode()).hexdigest())
        coletado = TextoColetado(fonte, novo, "", "text/html", "", "novo-hash", 0)
        self.assertFalse(texto_mudou("codigo-penal", coletado, hash_antigo))
        coletado.texto_normalizado += " Alteração substantiva"
        self.assertTrue(texto_mudou("codigo-penal", coletado, hash_antigo))

    def test_inline_nao_fragmenta_palavras_ou_referencias(self):
        html = (
            '<p>§ 1º (pela Medi<span>d</span>a Provisória)</p>'
            '<p>§ 2º art. 39, <i>caput</i>, incisos II e X<br>continuação</p>'
        )
        texto = normalizar_para_comparacao(html)
        self.assertIn("Medida Provisória", texto)
        self.assertIn("art. 39, caput, incisos II e X", texto)
        self.assertTrue(any(line.startswith("§ 1º") for line in texto.splitlines()))
        self.assertIn("continuação", texto.splitlines())

    def test_cdc_artigo_57_e_artefatos_derivados(self):
        fonte = (CDC / "fonte.txt").read_text(encoding="utf-8")
        soup = BeautifulSoup(fonte, "html.parser")
        paragrafo = next(p for p in soup.find_all("p") if "§ 2º Quando as condutas" in p.get_text())
        self.assertIn("<span", str(paragrafo))  # reprodução com a fonte versionada

        texto = normalizar_para_comparacao(fonte)
        salvo = (CDC / "texto.txt").read_text(encoding="utf-8")
        self.assertEqual(texto, salvo)
        art57 = texto.split("Art. 57.", 1)[1].split("Art. 58.", 1)[0]
        self.assertIn("Medida Provisória nº 1.393", art57)
        self.assertIn("art. 39, caput, incisos II e X", art57)
        self.assertNotIn("Medi\nd\na", art57)

        digest = hashlib.sha256(salvo.encode("utf-8")).hexdigest()
        meta = json.loads((CDC / "metadata.json").read_text(encoding="utf-8"))
        manifesto = json.loads((ROOT / "normas" / "manifesto.json").read_text(encoding="utf-8"))
        entrada = next(n for n in manifesto["normas"] if n["id"] == "codigo-defesa-consumidor")
        self.assertEqual(digest, (CDC / "sha256.txt").read_text(encoding="utf-8").strip())
        self.assertEqual(digest, meta["sha256_texto_normalizado"])
        self.assertEqual(len(salvo), meta["caracteres_texto_normalizado"])
        self.assertEqual(meta, entrada)


if __name__ == "__main__":
    unittest.main()
