# Instalação passo a passo

## Opção recomendada: somente pelo GitHub

### 1. Crie o repositório

No GitHub, crie um repositório novo, por exemplo:

`codigos-juridicos`

Pode ser privado ou público. Não é necessário criar README, `.gitignore` ou licença pelo assistente do GitHub, pois estes arquivos já integram o pacote.

### 2. Descompacte o pacote

Descompacte `codigos-juridicos-github.zip` no computador.

A estrutura precisa manter, inclusive, a pasta oculta `.github`:

```text
.github/
  workflows/
    atualizar-codigos.yml
config/
  normas.yml
normas/
  .gitkeep
scripts/
  atualizar_normas.py
.gitignore
INSTALACAO.md
README.md
requirements.txt
```

### 3. Envie tudo ao repositório

Pode usar a interface do GitHub ou Git.

Com Git instalado:

```bash
git clone URL_DO_SEU_REPOSITORIO
cd codigos-juridicos
```

Copie os arquivos deste pacote para dentro da pasta clonada e execute:

```bash
git add .
git commit -m "Instala atualizador automático de legislação"
git push
```

### 4. Confira a permissão do GitHub Actions

O workflow já declara:

```yaml
permissions:
  contents: write
```

Na maioria dos repositórios isso basta.

Se o primeiro `git push` feito pelo workflow for recusado, abra:

`Settings` → `Actions` → `General` → `Workflow permissions`

e permita leitura e escrita para o `GITHUB_TOKEN`, se a política da sua conta/organização autorizar.

Se a branch `main` estiver protegida contra pushes do GitHub Actions, ajuste a regra da branch ou use uma política de Pull Request.

### 5. Ative/teste o workflow

Abra:

`Actions` → `Atualizar códigos jurídicos` → `Run workflow`

A primeira execução instalará Python, Chromium e as bibliotecas necessárias.

### 6. Confira a primeira coleta

Depois de uma execução bem-sucedida, deverão existir:

```text
normas/codigo-civil/
normas/codigo-processo-civil/
normas/codigo-penal/
normas/codigo-processo-penal/
normas/manifesto.json
```

O workflow fará um commit porque os arquivos ainda não existiam.

### 7. Funcionamento diário

A partir daí:

- o GitHub dispara o workflow diariamente às 06:00 em `America/Sao_Paulo`;
- as quatro normas são consultadas;
- o texto de cada uma é validado;
- cada SHA-256 é comparado com a versão anterior;
- sem mudança textual: nenhum commit;
- com mudança: somente os arquivos das normas alteradas são regravados e versionados;
- com falha em uma fonte: nenhuma atualização parcial é gravada.

## Instalação e teste local no Windows 11

É opcional. O GitHub Actions funciona sem instalar Python no seu computador.

Se quiser testar localmente:

### 1. Instale Python 3.13

Durante a instalação, habilite a opção de adicionar o Python ao `PATH`.

### 2. Abra o PowerShell na pasta do projeto

Crie o ambiente virtual:

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
```

### 3. Instale as dependências

```powershell
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m playwright install chromium
```

### 4. Faça um teste sem gravar

```powershell
python scripts\atualizar_normas.py --dry-run
```

### 5. Faça a primeira gravação local, se quiser

```powershell
python scripts\atualizar_normas.py
```

## Adicionar mais normas

Você só precisa conhecer a URN LexML da nova norma.

Abra `config/normas.yml`, copie um bloco existente e altere:

- `id`;
- `titulo`;
- `urn`;
- `referencia`;
- parâmetros de validação.

O restante é automático.

## Ajustar o horário

No arquivo:

`.github/workflows/atualizar-codigos.yml`

altere:

```yaml
schedule:
  - cron: "0 6 * * *"
    timezone: "America/Sao_Paulo"
```

Exemplo: 05:30 diariamente:

```yaml
schedule:
  - cron: "30 5 * * *"
    timezone: "America/Sao_Paulo"
```

## Diagnóstico de falhas

### `HTTP 429`

A API limitou temporariamente as requisições. O script já faz novas tentativas com espera crescente.

### Não encontrou `/api/public/binario/.../texto`

O portal `normas.leg.br` pode ter alterado a forma de carregar o texto. Por segurança, nada será sobrescrito.

### Texto muito curto / fragmento não encontrado

A resposta recebida não passou pela validação jurídica mínima. O arquivo anterior foi preservado.

### Redução anormal

O texto novo ficou mais de 35% menor que o anterior. A atualização é bloqueada para evitar que uma página incompleta substitua um código válido.

### `git push` recusado

Confira `Settings` → `Actions` → `General` → `Workflow permissions` e as regras de proteção da branch.
