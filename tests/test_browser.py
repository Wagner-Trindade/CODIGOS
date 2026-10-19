"""Integração opcional: COLETOR_BROWSER=chrome usa o Chrome instalado.
Todas as respostas são locais e interceptadas; não consulta fontes oficiais.
"""
import copy
import os
import unittest
from scripts import atualizar_normas as c
from test_coleta import CFG, NORMA, TEXTO


@unittest.skipUnless(os.environ.get('COLETOR_BROWSER') in ('chrome', 'chromium'), 'Requer Chrome local e COLETOR_BROWSER=chrome')
class BrowserTest(unittest.TestCase):
    def test_resposta_invalida_seguida_por_texto_valido_no_navegador(self):
        cfg = copy.deepcopy(CFG)
        cfg['fontes']['normas_portal'] = 'https://coletor.test/'
        cfg['seguranca']['browser_timeout_ms'] = 6000
        with c.sync_playwright() as p:
            browser = p.chromium.launch(headless=True, **({'channel': 'chrome'} if os.environ['COLETOR_BROWSER'] == 'chrome' else {}))
            class BrowserLocal:
                def new_page(self):
                    page = browser.new_page()
                    def servir(route):
                        url = route.request.url
                        if '/api/public/binario/invalido/texto' in url:
                            route.fulfill(status=200, content_type='text/plain', body='Erro temporario')
                        elif '/api/public/binario/valido/texto' in url:
                            route.fulfill(status=200, content_type='text/plain', body=TEXTO)
                        else:
                            route.fulfill(status=200, content_type='text/html', body='''<body>Menu<script>
                            setTimeout(() => fetch('/api/public/binario/invalido/texto'), 100);
                            setTimeout(() => fetch('/api/public/binario/valido/texto'), 3500);
                            </script></body>''')
                    page.route('https://coletor.test/**', servir)
                    return page
            try:
                resultado = c.coletar_texto_normas(BrowserLocal(), NORMA, cfg, None)
                self.assertEqual(resultado.texto_normalizado, TEXTO.rstrip() + '\n')
                self.assertEqual(resultado.metodo_coleta, 'api-public-binario')
                self.assertEqual(len(browser.contexts), 0)
            finally:
                browser.close()
