# VCV Social Renderer

Serviço determinístico para produzir derivados editoriais da VCV. Não contém
integração com Meta, não publica conteúdo e não recebe prompts livres.

## Saídas v2

- `POST /v1/render/image`: WebP 1080×1350 para Feed/carrossel ou 1080×1920
  para Story/capa de Reel;
- `POST /v1/render/reel-package`: ZIP com MP4 H.264 1080×1920, capa WebP,
legenda SRT e manifesto;
- `GET /health`: versão do serviço e `publication=false`.

A versão 2 aplica o sistema visual derivado dos PDFs da Vanessa aos seis
templates cadastrados. Feed, Facebook, carrossel, Story, Reel e Marketplace
possuem composição explícita; Marketplace passa a gerar capa promocional sem
substituir as fotografias documentais do anúncio.

No Reel, cada cena aceita `motion` (`push_in`, `pull_out`, `pan_left` ou
`pan_right`) e `transition`. O pacote pode receber `music_profile` com
`ambient_warm`, `modern_soft`, `elegant_minimal` ou `none`. Os três perfis
musicais são sintetizados no render, sem gravações de terceiros, e a seleção é
registrada em `manifest.json`.

O serviço aceita somente o contrato conhecido, limita textos e quantidade de
imagens, exige HTTPS e allowlist exata em `SOURCE_IMAGE_HOSTS`, recusa redirects
e limita o download de cada fonte a `MAX_SOURCE_BYTES`.

## Build local

```bash
docker build -f implementation/social-renderer/Dockerfile \
  -t vcv-social-renderer:1.0.0 implementation
```

O contexto é `implementation` porque o build reutiliza somente o logo aprovado
da Watermark API e os arquivos do renderer. As fontes Poppins e Playfair
Display são baixadas do repositório oficial Google Fonts em commit fixado e
permanecem sob OFL.

## Teste

```bash
docker build --target test -f implementation/social-renderer/Dockerfile \
  -t vcv-social-renderer:test implementation
docker run --rm vcv-social-renderer:test
```

A etapa `test` inclui pytest; a imagem final `runtime` não inclui essa
dependência. Em runtime, configurar pelo menos:

```text
SOURCE_IMAGE_HOSTS=<host-ords>,<host-publico-backblaze>
MAX_SOURCE_BYTES=26214400
ALLOW_DRAFT_TEMPLATES=false
PORT=8080
```

Use `ALLOW_DRAFT_TEMPLATES=true` somente em homologação enquanto os
templates visuais ainda aguardam aprovação. Em produção, o renderizador
aceita exclusivamente templates com status `PUBLICADO`.

Não registrar tokens, URLs assinadas nem payloads completos em logs.
