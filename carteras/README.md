# carteras/

Cada archivo `<login>.json` es la cartera de IOL que esa persona sincronizó desde `cartera.html`, **cifrada**.
Se abre solo con su frase (o con la llave privada del motor, el secret `CARTERA_PRIVADA`).
La contraseña de IOL nunca llega acá: el navegador habla directo con IOL y no la guarda.
