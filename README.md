# RunToU Strava

Mini app local para conectar sua conta Strava e listar atividades recentes.

## Como usar

1. Copie `.env.example` para `.env`.
2. No Strava, clique em **Mostrar** no **Segredo do cliente**.
3. Coloque o valor no `STRAVA_CLIENT_SECRET`.
4. Rode:

```sh
python app.py
```

5. Abra:

```text
http://localhost:3000
```

## Configuração esperada no Strava

No cadastro do app Strava:

```text
Site: http://localhost:3000
Domínio de autorização callback: localhost
```

O app solicita `read,activity:read_all` para conseguir listar atividades recentes.
