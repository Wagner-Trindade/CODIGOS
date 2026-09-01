# Códigos Jurídicos Brasileiros — atualização automática

Projeto para manter, em um repositório GitHub, cópias versionadas de textos oficiais de normas jurídicas brasileiras.

## Normas cadastradas

O rol vigente está em `config/normas.yml` (entrada) e em `normas/manifesto.json` (saída, com número, URN, SHA-256 e data da última alteração detectada de cada norma). Não há lista duplicada neste README, para que não exista uma segunda versão da verdade.

O rol é expansível pelo `config/normas.yml`, sem alteração do código Python nem do workflow.

## Fontes

O projeto usa duas camadas oficiais:

1. **API de Dados Abertos do Senado Federal**, consultada pela URN LexML da norma, para validação e metadados estruturados.
2. **Portal `normas.leg.br`**, para o texto exibido pelo sistema oficial. Como o portal é uma aplicação JavaScript, o script abre a página com Chromium/Playwright e captura a própria resposta pública de texto utilizada pela aplicação.

Não há tentativa de burlar CAPTCHA, Cloudflare, bloqueio do Planalto/Câmara, rotação de IP, proxy ou falsificação de origem.

## Segurança jurídica da atualização

Antes de gravar qualquer coisa, o script:

- consulta e valida **todas** as normas cadastradas;
- exige fragmentos inequívocos do título/número;
- exige tamanho mínimo;
- exige quantidade mínima de artigos;
- rejeita redução textual anormal em relação à versão anterior;
- calcula SHA-256 do texto normalizado;
- somente grava se o hash mudou.

A atualização é **transacional**: se uma única norma falhar, nenhuma das demais é gravada. A versão válida anterior permanece intacta.

## Arquivos gerados

Na primeira execução serão criadas pastas como:

```text
normas/
├── codigo-civil/
│   ├── texto.txt
│   ├── fonte.txt
│   ├── metadata.json
│   ├── metadata-senado.json
│   └── sha256.txt
├── codigo-processo-civil/
├── codigo-penal/
├── codigo-processo-penal/
└── manifesto.json
```

## Contrato de acesso

Índice oficial: **`normas/manifesto.json`**. É o único índice do repositório, gerado pelo próprio script a cada execução bem-sucedida.

O caminho de qualquer norma é determinístico, sem adivinhação de nome de pasta:

```text
normas/manifesto.json  →  normas[].id  →  normas/{id}/texto.txt
```

O campo `id` é validado contra `[a-z0-9][a-z0-9-]*` e é usado literalmente como nome do diretório. Exemplo: `id: "codigo-processo-civil"` → `normas/codigo-processo-civil/texto.txt`.

Para consumo programático, o `raw` do arquivo é:

```text
https://raw.githubusercontent.com/Wagner-Trindade/CODIGOS/main/normas/manifesto.json
```

`fonte.txt` conserva a resposta textual recebida da fonte oficial. `texto.txt` normaliza apenas a representação técnica (HTML, Unicode, finais de linha e espaços finais), sem resumir os dispositivos. O histórico das versões anteriores fica no próprio Git.

## Horário

O workflow está configurado para:

```yaml
cron: "23 5 * * *"
timezone: "America/Sao_Paulo"
```

ou seja, diariamente às 05:23 no fuso de São Paulo.

O GitHub informa que workflows agendados podem sofrer atraso em períodos de alta carga. Para atualização legislativa diária isso normalmente é aceitável; se a execução precisar ocorrer em segundo exato, deve-se usar um scheduler externo.

## Execução manual

No GitHub:

`Actions` → `Atualizar códigos jurídicos` → `Run workflow`.

Localmente:

```bash
python scripts/atualizar_normas.py
```

Teste sem gravar:

```bash
python scripts/atualizar_normas.py --dry-run
```

## Como adicionar outra norma

Edite `config/normas.yml` e acrescente um item:

```yaml
- id: "exemplo"
  titulo: "Nome da norma"
  urn: "urn:lex:..."
  referencia:
    tipo: "Lei"
    numero: "00.000"
    ano: 2000
  validacao:
    fragmentos_obrigatorios:
      - "00.000"
      - "Nome da norma"
    minimo_caracteres: 5000
    minimo_artigos: 1
```

Na execução seguinte, a nova pasta será criada automaticamente.

## Política de falha

Se a fonte oficial mudar de estrutura, ficar indisponível ou devolver conteúdo incompleto, o job termina com erro e **não sobrescreve os textos jurídicos já armazenados**. Isso é deliberado.
