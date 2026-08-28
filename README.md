# Watermark API com suporte HEIC

Versão derivada do ZIP recebido em 28/08/2026. O original permanece somente
como referência fora do repositório.

## Alterações

- registra `pillow-heif` para abrir HEIC/HEIF;
- corrige orientação EXIF antes da marca d'água;
- preserva a saída WebP esperada pelo workflow n8n;
- devolve `watermarked.webp`, evitando reaproveitar a extensão `.heic`;
- limita uploads a 25 MB e retorna erros JSON 400/413/415/422/500;
- adiciona `GET /health`;
- usa Gunicorn no lugar do servidor de desenvolvimento do Flask.

## Publicação no Easypanel

1. Faça backup ou registre a revisão atualmente publicada.
2. Substitua `Dockerfile`, `app.py` e `requirements.txt` pelos arquivos desta
   pasta e mantenha `logo.png` original ao lado deles.
3. Reconstrua/republique o serviço.
4. Confirme `GET /health` com HTTP 200.
5. Teste primeiro JPEG e depois HEIC em homologação.
6. Confirme no n8n que o retorno possui MIME `image/webp` e nome
   `watermarked.webp` antes do envio ao bucket.

## Rollback

Restaure a revisão/imagem anterior do serviço no Easypanel. O contrato de
sucesso continua sendo um arquivo WebP, portanto nenhum ajuste no banco é
necessário para reverter.
