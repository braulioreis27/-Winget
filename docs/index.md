# WPM - Gerenciador de pacotes para Windows

CLI em Python que envelopa o **winget** para gerenciar o ciclo de vida de
softwares no Windows: procurar, baixar, instalar, atualizar, remover e limpar
resíduos. Interface em português, com cores, tabelas, barra de progresso
(percentual quando disponível e bytes/velocidade durante downloads),
confirmações, tratamento de UAC e logs.

## Funcionalidades

| Comando | O que faz |
| --- | --- |
| `wpm search <termo>` | Pesquisa pacotes nas fontes do winget (winget/msstore) |
| `wpm download <pacote>` | Baixa o instalador e gera SHA-256 dos arquivos |
| `wpm install <pacote>` | Instala silenciosamente ou de forma guiada (`--interactive`) |
| `wpm list` / `wpm list -u` | Lista instalados ou somente atualizações disponíveis |
| `wpm update <pacote>` / `--all` | Atualiza pacotes com relatório por item |
| `wpm remove <pacote>` | Desinstala e, opcionalmente, limpa resíduos (`--purge`) |
| `wpm clean <nome>` | Procura dados residuais em AppData/ProgramData/Program Files |
| `wpm doctor` | Diagnóstico de winget, rede, permissões, fontes e pastas |
| `wpm config` | Mostra/altera preferências persistentes |
| `wpm` | Abre o menu interativo |

## Requisitos

- Windows 10/11 com **winget** (App Installer) 1.6 ou superior.
  O download usa `winget download`, disponível a partir da 1.6.
- Python 3.9+.
- Não é necessário administrador para uso geral. Operações de máquina podem
  exigir UAC; use `--elevate` para relançar o comando elevado.

## Como executar

No diretório do projeto:

```powershell
py -3 run.py --help
# ou
python -m wpm --help
# ou, no Explorer/CMD:
wpm.cmd install 7zip.7zip
```

Instalação opcional como comando global:

```powershell
py -3 -m pip install -e .
wpm --help
```

## Exemplos

```powershell
wpm search firefox
wpm install 7zip.7zip
wpm install "Google Chrome" --scope user
wpm download "K-Lite Codec Pack" --dir D:\Instaladores
wpm list --upgradable
wpm update --all
wpm update NanaZip --interactive
wpm remove firefox --purge
wpm clean "K-Lite" --dry-run
wpm clean 7zip.7zip --permanent
wpm doctor
wpm config --set download_dir=D:\Downloads\wpm
```

Opções úteis:

- `--dry-run`: mostra o que seria feito, sem instalar/baixar/remover.
- `--interactive`: usa o instalador/desinstalador com interface do programa.
- `--elevate`: relança o comando via UAC em uma nova janela.
- `--purge`: após desinstalar, procura e remove dados residuais.
- `--permanent`: exclui resíduos definitivamente (padrão: Lixeira).
- `--no-system`: limita a busca de resíduos a AppData (sem Program Files).
- `-y/--yes`: confirma automaticamente (use com cuidado).
- `-v/--verbose`: mostra detalhes e comandos executados.

## Segurança e resiliência

- **Downloads**: o winget valida o hash do instalador no momento da
  instalação. O WPM calcula SHA-256 dos arquivos baixados e grava um arquivo
  `.sha256` na pasta de destino para auditoria.
- **Remoção de resíduos**: a busca exige correspondência com o nome do
  aplicativo (nunca apenas com o fornecedor), ignora links/junções, protege
  raízes de sistema, o perfil do usuário e o diretório atual. Por padrão os
  itens vão para a **Lixeira**; `--permanent` pede a digitação de `EXCLUIR`
  em terminais interativos (mesmo com `--yes`). Em modo não interativo, use
  `--yes --permanent` apenas em scripts confiáveis.
- **UAC**: se uma operação exigir administrador, o WPM avisa. `--elevate`
  abre uma nova janela elevada e aguarda o término. O winget também pode
  disparar o UAC do instalador.
- **Erros**: falhas de rede, pacote inexistente, timeout e códigos HRESULT
  do winget são traduzidos em mensagens claras. A saída completa fica nos logs.
- **Não interativo**: use `--yes` em scripts; sem terminal, confirmações
  obrigatórias são recusadas em vez de assumidas.

## Configuração e logs

- Configuração: `%APPDATA%\wpm\config.json`
- Logs rotativos: `%LOCALAPPDATA%\wpm\logs\wpm.log`
- Download padrão: `%USERPROFILE%\Downloads\wpm`

Chaves disponíveis: `silent_install`, `checksum_after_download`,
`download_dir`, `source`, `update_sources`, `color`, `timeout`,
`search_limit`, `log_level`.

## Códigos de saída

| Código | Significado |
| --- | --- |
| 0 | sucesso |
| 1 | erro inesperado |
| 2 | cancelado pelo usuário ou uso inválido dos argumentos |
| 3 | winget não encontrado |
| 4 | privilégios insuficientes |
| 5 | falha de rede |
| 6 | pacote não encontrado |
| 7 | operação do winget falhou |
| 8 | configuração inválida |
| 130 | interrompido com Ctrl+C |

## Estrutura

```
wpm/
  cli.py          argumentos, serviços e menu interativo
  winget.py       comandos e parsing das tabelas do winget
  executor.py     subprocessos com streaming e timeout
  ui.py           cores, tabelas, prompts e barra de progresso
  cleanup.py      varredura segura de resíduos
  platform_win.py UAC, ANSI, Lixeira e descoberta do winget
  config.py       preferências e logging
  models.py       dataclasses compartilhadas
  errors.py       exceções e códigos de saída
run.py            atalho para executar sem instalar
wpm.cmd           atalho para CMD/PowerShell
tests/            testes unitários (unittest)
```

## Testes

```powershell
py -3 -m unittest discover -s tests -v
```
