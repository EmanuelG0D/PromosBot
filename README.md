# Radar de Ofertas — USA + Colombia

Monitor automático de gangas que corre en piloto automático y avisa por Telegram:
ofertas, cupones, rebajas y combinaciones de rebaja + cupón. Costo de
infraestructura: **$0**.

- **Sin dependencias**: solo la librería estándar de Python 3.10+. No hay `pip install`.
- **Sin scraping frágil**: consume feeds y APIs públicas, no parsea HTML de tiendas.
- **Con memoria**: guarda el historial de precios que él mismo observa, así distingue
  una rebaja real de un precio de lista inflado.

---

## Qué vigila

| Fuente | Qué trae | Cómo |
|---|---|---|
| **Slickdeals** | Ofertas de EE. UU. ya filtradas por la comunidad (Amazon, eBay, Nike, Walmart…) | RSS público. Nadie bloquea un lector de RSS. |
| **Alkosto · K-tronix** | Catálogo + **precio exclusivo con tarjeta** de la tienda | Índice Algolia del propio buscador |
| **Éxito · Carulla · Olímpica** | Catálogo colombiano ordenado por mayor descuento | API JSON de VTEX |
| **Falabella · Homecenter** | Catálogo de tecnología, moda, ferretería y hogar | JSON incrustado de Next.js |
| **Mercado Libre Colombia** | Liquidaciones del día, cupones activos y tiendas oficiales | Centro oficial de ofertas (SSR JSON pre-renderizado) |
| **PROMOCAJITA** | Ofertas destacadas de la comunidad colombiana | Canal público de Telegram |
| **eBay** *(opcional)* | Outlets oficiales y reacondicionados certificados | Browse API oficial (5.000 llamadas/día gratis) |

En cada alerta el bot añade:

- **Cupones ocultos** extraídos del texto (`w/ code`, `use promo code`, `cupón…`),
  formateados como bloque de código: en Telegram móvil se copian con un toque.
- **Costo puesto en Colombia** para lo de EE. UU.: precio + flete de casillero,
  convertido a pesos con la **TRM oficial** del día, indicando si el envío queda
  bajo el límite de USD 200 que lo exime de IVA y arancel.
- **Promociones bancarias** que la tienda exige para el precio anunciado.

---

## Cómo llega cada oferta

**Una tarjeta por oferta, con la foto del producto.** No hay listas ni resúmenes:
cada ganga llega como su propio mensaje, con imagen, precio tachado, cupón
copiable y el enlace directo.

```
[ foto del producto ]
🔥 Alkosto · -65%
Audífonos SONY WH-CH720N Cancelación de Ruido
💵 $999.900 → $349.900
📉 Mínimo de 30 días: $420.000 (hoy baja de ahí)
🎟 Cupón: SAVE10  (tócalo para copiarlo)
🔗 Abrir oferta
```

Si una imagen falla o la tienda no la publica, el mensaje sale igual como texto
y Telegram arma su propia vista previa del enlace. Nunca se pierde una oferta
por culpa de una foto.

### El control de volumen

Sin resumen, lo que evita el spam son otras tres cosas:

| Mecanismo | Qué hace |
|---|---|
| `MAX_ALERTS_PER_RUN` (8) | Tope de tarjetas por ronda |
| Deduplicación | Una oferta ya avisada no se repite hasta que baje otro 10% o pasen 14 días |
| Ronda inicial | La primera vez archiva el catálogo como línea base en vez de anunciarlo entero |

En régimen normal solo llega **lo que cambió** desde la ronda anterior, que son
unas pocas por hora. Si algún día el canal se vuelve inmanejable, existe el
resumen agrupado: enciéndelo con `DIGEST_ENABLED=true` y las ofertas menores se
juntarán en un solo mensaje cada `DIGEST_EVERY_HOURS`.


### Qué trae de EE. UU. y qué no

Slickdeals es un foro donde cabe de todo, así que la fuente se filtra en tres
capas dentro de `watchlist.json`:

| Capa | Para qué |
|---|---|
| `queries` | Qué buscar. La consulta especial **`portada`** no busca nada: lee el feed de primera página, o sea las ofertas que la comunidad votó hacia arriba. |
| `incluir` | El título debe mencionar algo de esta lista (marcas, consolas, tecnología, hogar). Si no, se descarta. |
| `excluir` | Veto directo: perfumes, tarjetas de regalo, vitaminas, suplementos, seguros, mercado. |

Medido en una ronda real: de 170 resultados crudos pasaron 135, y del feed de
portada solo 3 de 25 coincidían con los intereses configurados. Lo descartado
eran cepillos de auto, cajas plásticas, ropa interior, vestidos y cerraduras.

Para afinarlo, edita esas tres listas. Si quieres que de adidas solo lleguen
zapatos y no medias ni loncheras, cambia la consulta `"adidas"` por
`"adidas shoes"`.

### El freno diario

`MAX_ALERTS_PER_DAY` (40 por defecto) es el tope de mensajes en 24 horas, sin
importar cuántas ofertas aparezcan. Con rondas cada hora, es lo que separa
un bot útil de uno insoportable. El contador vive en la base de datos, así que
sobrevive a los reinicios mientras el respaldo esté configurado.

### Cada cuánto busca

**Dos ritmos, porque no todo cambia al mismo paso.**

| Ronda | Fuentes | Cada | Peticiones |
|---|---|---|---|
| Comunidad | Slickdeals, PROMOCAJITA | 15 min | 23 |
| Catálogos | Las 12 tiendas y la droguería | 1 hora | 141 |

Las ofertas de comunidad duran horas y hay que cazarlas pronto; el precio de un
televisor en Éxito no cambia en quince minutos. Preguntarle a todo cada cuarto
de hora eran **15.744 peticiones diarias** para enterarse de lo mismo; así son
**4.194**, un 73% menos, sin perder nada que importe.

Una ronda de catálogos revisa ~4.200 ofertas y tarda unos 40 segundos, así que
el servicio pasa la mayor parte del tiempo en reposo.

### Horario: de 6 a. m. a medianoche

De madrugada las tiendas colombianas no publican nada, así que el bot no sale a
buscar entre las 12 y las 6 (`RONDAS_DESDE_HORA` / `RONDAS_HASTA_HORA`, en hora
de Colombia). Dormir esas seis horas deja margen de sobra dentro de las 750
horas gratis de Render.

El horario se comprueba **dentro del servicio**, no solo en el ping: un comando
de madrugada despierta la instancia, y sin esa guarda arrancaría una ronda
completa contra las doce tiendas a las tres de la mañana. Los comandos sí
responden a cualquier hora — lo que se pausa es la búsqueda automática.

### La primera ronda es distinta

Con la base vacía, *todo* el catálogo parece nuevo: en una prueba real dieron
715 candidatas de golpe. Por eso la primera ronda manda solo las 5 mejores y
**archiva el resto como línea base**. A partir de ahí el bot avisa de lo que
*cambia*, que es su trabajo real.

Esto vuelve a ocurrir si Render reinicia sin el respaldo en GitHub configurado.

---

## Objetivos de precio: lo que el porcentaje no detecta

Este es el mecanismo clave. Una freidora a $99.900 puede aparecer como "-45%" y
perderse entre cientos de rebajas mediocres, pero **ese precio absoluto** es
justo la ganga que interesa. Un objetivo lo dice sin rodeos:

```json
"objetivos": [
  { "termino": "freidora",  "max_cop": 150000 },
  { "termino": "audifonos", "max_cop": 90000, "excluir": ["forro", "estuche", "cable"] },
  { "termino": "sneakers",  "max_usd": 35 }
]
```

Cuando algo cruza por debajo del tope, entra como **aviso inmediato**, sin
importar qué porcentaje declare la tienda.

Dos detalles que hacen que funcione:

- **El término se agrega solo a las búsquedas.** Fijar un objetivo de "freidora"
  sin buscar freidoras no serviría de nada, así que el bot inyecta el término en
  las consultas de las tiendas correspondientes (`max_cop` → tiendas colombianas,
  `max_usd` → Slickdeals y eBay).
- **Filtra accesorios.** Buscar "freidora" también devuelve canastas y repuestos.
  Hay una lista por defecto de palabras de accesorio, y `excluir` la reemplaza
  cuando quieras afinarla por producto.

---

## Puesta en marcha (5 minutos)

### 1. Crear el bot de Telegram

1. En Telegram, escribe a **@BotFather** → `/newbot` → copia el token.
2. Pega el token en `.env`, en `TELEGRAM_BOT_TOKEN`.
3. Agrega el bot al **grupo** (o canal) donde quieres recibir las ofertas.
4. Pregúntale al propio bot cuál es el id de ese chat:

```bash
python radar.py --chat-id
```

Copia el número que salga —siempre **negativo** en grupos— a `TELEGRAM_CHAT_ID`.

Detalles que confunden a todo el mundo:

- **En un grupo normal el bot no necesita ser administrador** para escribir. Solo
  hazlo admin si el grupo tiene activado "solo los administradores pueden enviar
  mensajes". En un **canal** sí debe ser administrador siempre.
- **El modo privacidad** hace que el bot no vea los mensajes corrientes del grupo.
  No afecta el envío de ofertas; solo significa que, si `--chat-id` sale vacío,
  debes escribir `/start@TuBot` en el grupo para que el bot registre el chat.
- **Si el grupo se convierte en supergrupo** (al hacerlo público o crecer), su id
  cambia de `-123456789` a `-100123456789` y el bot se queda mudo. Pasa de
  verdad: le ocurrió a este proyecto. El bot lo detecta y lo grita en el log
  con el id nuevo — pero hay que actualizarlo **en los dos lados**: el `.env`
  local y el secret `TELEGRAM_CHAT_ID` del repositorio.

### 2. Configurar

```bash
cp .env.example .env
```

Edita `.env` con el token y el chat id. Verifica la conexión:

```bash
python radar.py --test-telegram
```

### 3. Probar sin enviar nada

```bash
python radar.py --dry-run
```

Imprime en consola exactamente lo que enviaría. Cuando el resultado te guste:

```bash
python radar.py
```

---

## Desplegar en Render (plan gratuito) + ping

Render no ofrece cron jobs ni background workers gratis, y **apaga los servicios
gratuitos tras 15 minutos sin tráfico entrante**. Por eso el radar se despliega
como *web service* ([server.py](server.py)): un hilo interno corre las rondas y
un ping externo lo mantiene despierto.

### 1. Subir el proyecto a GitHub

Un repositorio **privado** (`.env` y `radar.db` ya están en `.gitignore`).

### 2. Crear el servicio en Render

*New → Blueprint* y apuntar al repositorio. El archivo
[render.yaml](render.yaml) lo configura solo. Luego, en *Environment*, agrega:

| Variable | Valor |
|---|---|
| `TELEGRAM_BOT_TOKEN` | el de @BotFather |
| `TELEGRAM_CHAT_ID` | el de tu canal |
| `RUN_TOKEN` | una palabra secreta cualquiera (protege `/run`) |

### 3. Los comandos se conectan solos

No hay nada que configurar: al arrancar, el servicio lee `RENDER_EXTERNAL_URL`
(que Render inyecta solo), registra el webhook en Telegram y publica el menú de
comandos. Se comprueba en `/status`, campo `webhook`.

Cada entrega viene firmada con un secreto que Telegram devuelve en la cabecera
`X-Telegram-Bot-Api-Secret-Token`; si no coincide, el servidor responde 403. El
secreto sale del token del bot, así que tampoco hay que inventarlo — salvo que
prefieras fijar `TELEGRAM_WEBHOOK_SECRET` a mano.

### 4. Configurar el ping cada 5 minutos

Con **cron-job.org** o **UptimeRobot** (ambos gratis), apuntando a:

```
https://TU-SERVICIO.onrender.com/healthz
```

Cada 5 minutos, **de 6 a. m. a medianoche** (los dos permiten poner horario).
Con eso nunca pasa 15 minutos en silencio y no se duerme mientras trabaja.

> El ping **no** ejecuta rondas: solo despierta el servicio y responde al
> instante, así el pinger nunca se topa con un timeout. Las rondas las disparan
> los dos relojes internos, cada uno con su intervalo.

De noche el servicio se duerme a propósito y no pasa nada: los comandos siguen
funcionando porque el webhook de Telegram lo despierta solo, con un minuto de
arranque en frío. Lo único que se pausa es la búsqueda automática.

### 5. Hacer que el historial sobreviva (importante)

**El plan gratuito de Render no tiene discos persistentes**: `radar.db` se borra
en cada despliegue y en cada reinicio. Sin esto, el bot olvida qué ya avisó y
**repite las mismas ofertas**.

La solución sin costo es guardar la base en tu propio repositorio:

1. En GitHub: *Settings → Developer settings → Personal access tokens → Fine-grained*.
2. Dale acceso **solo a ese repositorio**, con permiso **Contents: Read and write**.
3. En Render agrega `GITHUB_TOKEN` (el token) y `GITHUB_REPO` (`usuario/repositorio`).

El bot restaura el historial al arrancar y lo guarda al final de cada ronda.
Si no configuras esto, todo funciona igual, pero verás ofertas repetidas cada
vez que Render reinicie el servicio.

### Endpoints

| Ruta | Para qué |
|---|---|
| `/` | Estado en JSON: rondas hechas, última ronda, próxima, errores |
| `/healthz` | Respuesta mínima para el pinger |
| `/run?token=…` | Dispara una ronda ya, sin esperar al reloj |

### El límite de horas

Render regala **750 horas de instancia al mes por workspace**. Mantener esto
encendido 24/7 consume ~744 h en un mes de 31 días: **cabe, pero sin margen**.
Si tienes otro servicio gratuito encendido, se agotan y Render los suspende
hasta el mes siguiente. Si quieres margen, haz que el pinger solo trabaje de
6 a. m. a medianoche: son ~558 h.

---

## Otras formas de dejarlo corriendo

### GitHub Actions (descartado)

El proyecto nació corriendo en Actions y **se retiró**: el `schedule` de GitHub
no es una garantía, sino un mejor esfuerzo que en repos públicos gratuitos se
descarta casi siempre. Medido en este repo: de ~93 disparos esperados en 8
horas, ocurrió 1. Los disparos manuales (`workflow_dispatch`) sí arrancan sin
cola, así que un cron externo llamando a la API de GitHub funcionaría — pero si
ya hay un servicio encendido en Render, montar un segundo reloj encima sobra.

### Teléfono Android con Termux

```bash
pkg install python git
```

```bash
while true; do python radar.py; sleep 1800; done
```

---

## Qué vigilar: `watchlist.json`

Todo se configura ahí, sin tocar código: `objetivos` (precios tope) y `queries`
(términos de búsqueda por fuente). Cada término es una búsqueda independiente;
entre más términos, más lenta la ronda — Alkosto/K-tronix agrupan todas las
consultas en una sola petición, así que ahí puedes ser generoso.

## Umbrales: `.env`

| Variable | Por defecto | Qué hace |
|---|---|---|
| `MIN_DISCOUNT_PCT` | 40 | Piso para que una oferta exista (entra al resumen) |
| `INSTANT_DISCOUNT_PCT` | 60 | Desde aquí interrumpe con mensaje propio |
| `DIGEST_ENABLED` | false | Agrupar las menores en un resumen en vez de mandarlas sueltas |
| `DIGEST_EVERY_HOURS` | 12 | Cada cuánto saldría ese resumen |
| `DIGEST_MAX_ITEMS` | 20 | Cuántas ofertas caben en cada resumen |
| `SEED_ON_EMPTY_DB` | true | Primera ronda: avisa solo lo mejor y archiva el resto como línea base |
| `SEED_MAX_ALERTS` | 5 | Cuántas alertas manda esa primera ronda |
| `VERACITY_WINDOW_DAYS` | 30 | Ventana para calcular el mínimo real |
| `VERACITY_MIN_DAYS` | 7 | Historial mínimo antes de emitir veredicto |
| `VERACITY_MIN_DROP_PCT` | 5 | Cuánto debe bajar del mínimo real para avisar |
| `HIKE_PCT` | 10 | Subida que delata el inflado previo |
| `FLASH_WINDOW_HOURS` | 48 | Vencimiento que convierte una oferta en urgente |
| `SUPPRESS_FAKE_DISCOUNTS` | true | Silenciar rebajas que no mejoran el mínimo |
| `GLITCH_DISCOUNT_PCT` | 75 | A partir de aquí se marca como posible error de precio |
| `MAX_ALERTS_PER_RUN` | 8 | Tope de tarjetas por ronda |
| `MAX_ALERTS_PER_DAY` | 40 | Tope de tarjetas en 24 horas |
| `REALERT_DROP_PCT` | 10 | Cuánto debe bajar algo ya avisado para volver a avisarlo |
| `REALERT_DAYS` | 14 | Cada cuánto se puede repetir una oferta que sigue vigente |
| `REQUIRE_HISTORY_FOR_GLITCH` | false | Exige historial propio antes de gritar "error de precio" |
| `FREIGHT_USD_PER_LB` | 4.0 | Tarifa de tu casillero, por libra |
| `DEFAULT_WEIGHT_LB` | 2.0 | Peso asumido cuando no se conoce (calzado/ropa) |

### Cómo elegir los umbrales

Medido sobre el catálogo real de las cinco tiendas colombianas (585 productos
con precio de lista confiable), esto es lo que entra por ronda:

| Umbral | Productos únicos |
|---|---|
| 40% | 158 — el piso del resumen |
| 50% | 86 |
| 55% | 55 |
| **60%** | **22 — el corte de los avisos inmediatos** |
| 65% | 6 |
| 70% | 3 |
| 75% | 0 |

Que no haya *ninguno* por encima del 75% confirma el punto de la siguiente
sección: todo lo que parecía un error de precio venía de vendedores externos
con el precio de lista inflado.

---

## Por qué "75% de descuento = error de precio" no basta

Es la trampa más grande de este tipo de bots, y vale la pena entenderla.

Las tiendas VTEX permiten que vendedores externos (*marketplace*) publiquen el
precio de lista que quieran. Al pedir el catálogo ordenado por mayor descuento,
lo primero que llega es basura: un portátil "de $36.799.990" rebajado a
$1.679.990 — un 95% de descuento que nunca existió, porque el precio real
siempre fue $1.679.990.

En la primera prueba real, **las 4 mejores "gangas" eran exactamente eso**.

El radar lo resuelve así:

1. **El precio de lista de un tercero no decide nada.** Si el vendedor no es la
   tienda misma, o si el precio de lista supera 10 veces el de venta, ese
   porcentaje se ignora por completo.
2. **Esos productos no se descartan: quedan en observación.** El bot registra su
   precio y construye su propia referencia.
3. **Cuando algo cae frente a esa referencia propia, ahí sí alerta**, y lo marca
   con confianza alta: es una caída medida por el bot, no declarada por el vendedor.

Con este filtro, una ronda de prueba pasó de 314 "ofertas" a 54 reales.

---

## Ofertas relámpago, trasnochones y envío gratis

Las tiendas VTEX exponen dos campos que revelan justo esto:

- **`PriceValidUntil`** — el precio tiene *hora de caducidad*. Un precio que
  vence esta madrugada es un trasnochón; uno que vence en dos horas es un
  relámpago. Cuando faltan menos de `FLASH_WINDOW_HOURS` (48 por defecto), la
  oferta **salta directo a aviso inmediato**: si esperara al resumen, ya habría
  vencido. El mensaje lo dice: *"⏳ Termina en menos de 1 hora (hasta las 23:59)"*.
- **`clusterHighlights`** — las campañas y beneficios activos. De ahí salen
  **Envío gratis**, **0% interés con Davivienda / Occidente / Tuya**,
  **Días Amarillos**, **Cyber**, **Black Friday**, **Financiación con Addi**.

VTEX mezcla ahí sus nombres internos de campaña (`reindex total`,
`cont-pequenos-4431`), así que el bot usa **lista blanca**: solo muestra lo que
reconoce y descarta el resto en silencio.

### Lo que sí y lo que no

| Señal | Estado |
|---|---|
| Relámpagos y trasnochones (Éxito, Carulla, Olímpica) | ✅ vía `PriceValidUntil` |
| Envío gratis y promos bancarias (VTEX) | ✅ vía `clusterHighlights` |
| Precio exclusivo con tarjeta (Alkosto, K-tronix) | ✅ vía `paymentpromotion` |
| Cupones con código (Slickdeals / EE. UU.) | ✅ extraídos del texto |
| **Cupones con código en tiendas colombianas** | ❌ **no los publican en el catálogo** |
| Envío gratis en Alkosto/K-tronix | ❌ el único campo (`hasKasadoNotFree`) es ambiguo |

Sobre los cupones colombianos: no es una limitación del bot sino de las tiendas.
Éxito, Alkosto y compañía no exponen códigos de cupón en su API de catálogo —
viven en banners y en el checkout. Lo que sí exponen, y el bot ya captura, son
las promociones bancarias y el envío gratis.

---

## Mercado Libre: cómo se lee el catálogo y los cupones

A diferencia de la búsqueda abierta (`listado.mercadolibre.com.co`), que presenta CAPTCHAs, o su API REST tradicional (`api.mercadolibre.com`), que responde `403 PolicyAgent` a clientes no certificados, Mercado Libre cuenta con un centro público oficial de ofertas en Colombia:

```
https://www.mercadolibre.com.co/ofertas
```

El bot consulta directamente este hub oficial (y sus categorías específicas como `MCO1000`, `MCO1051`, `MCO1648`, `MCO1276`, etc.):

- **Sin navegadores pesados ni APIs con permisos**: lee el estado inicial SSR (`_n.ctx.r`) que los servidores de Mercado Libre inyectan directamente en el HTML.
- **Cupones oficiales**: extrae promociones y cupones activos (ej. `🎟️ Cupón 10% OFF`) autorizados en la pasarela de Mercado Libre.
- **Precios reales**: extrae el precio de venta y el `previous_price` certificado para calcular el porcentaje de descuento verificable.
- **Imágenes en alta resolución**: mapea las fotos al CDN nativo `D_NQ_NP_{id}-F.jpg`, garantizando que Telegram las envíe nítidas sin compresión excesiva.
- **Vendedores verificados**: captura el nombre del comercio oficial cuando la venta proviene de una marca certificada.

---

## La prueba de honestidad: el mínimo de 30 días

La trampa más común del retail no es el error de precio: es **subir el precio
dos semanas antes de la promoción para luego "rebajarlo"**. La tienda anuncia
-49% contra un precio de lista que nadie pagó nunca.

Contra eso, el porcentaje que declara la tienda no sirve de nada. Solo sirve una
cosa: **el precio más bajo que el producto tuvo realmente en los últimos 30
días**, medido por el propio bot. Es el mismo criterio que la directiva europea
Omnibus le impone a las tiendas. Si la "oferta" no baja de ese mínimo, no es una
oferta.

El bot lo aplica en las dos direcciones:

| Situación | Qué anuncia la tienda | Qué hace el bot |
|---|---|---|
| Valía $300.000, lo suben a $500.000 y lo "rebajan" a $460.000 | **-49%** | **Silencia la alerta**: *"subieron el precio 66,7% antes de la rebaja"* |
| Valía $300.000 y baja de verdad a $250.000 | -21,9% | **Alerta 🔥**: *"Mínimo de 30 días: $300.000 (hoy baja de ahí)"* |

Fíjate en la segunda fila: la rebaja real se anunciaba **peor** que la falsa. Por
eso el bot avisa aunque el descuento publicado sea modesto — una caída medida
por él pesa más que cualquier cifra declarada por la tienda.

Cuando detecta que hubo un alza previa, lo dice en el mensaje: *"Le subieron
66,7% antes de anunciar la rebaja."*

### La limitación honesta

**Esto solo funciona con historial acumulado.** Los primeros días el veredicto
es "sin datos" y el bot depende de sus otras señales. Necesita al menos
`VERACITY_MIN_DAYS` (7 por defecto) y 3 observaciones del producto antes de
opinar — antes de eso, cualquier veredicto sería inventado.

No hay atajo: no existe una fuente pública y gratuita de historial de precios
para el retail colombiano (`knasta.co` ya ni siquiera resuelve; solo sobrevive
el `.cl` de Chile). El historial hay que construirlo.

Esto hace que **el respaldo en GitHub sea crítico si despliegas en Render**: sin
él, cada reinicio borra el historial y el bot vuelve a quedar ciego ante esta
trampa.

---

## Comandos: pedirle ofertas cuando quieras

Además de avisar solo, el bot responde comandos escritos en el grupo:

| Comando | Qué hace |
|---|---|
| `/mercadolibre` `/meli` | Lo mejor de Mercado Libre Colombia y sus cupones |
| `/alkosto` `/ktronix` | Lo mejor de Alkosto y K-tronix ahora mismo |
| `/exito` `/carulla` `/olimpica` | Ofertas de las tiendas de Grupo Éxito y Olímpica |
| `/falabella` `/homecenter` | Ofertas de Falabella y Homecenter |
| `/drogueria` | Rebajas de droguerías (La Rebaja) |
| `/cajita` | Lo que publica la comunidad PROMOCAJITA |
| `/colombia` | Lo mejor de las tiendas colombianas juntas |
| `/exterior` | Ofertas de EE. UU. filtradas a tus intereses (Slickdeals) |
| `/todo` | Colombia y exterior mezclados |
| `/menu` | Abre el teclado interactivo con botones |
| `/ayuda` | La lista completa |

### Menú interactivo y navegación por departamentos

Además de escribir comandos con `/`, el bot cuenta con un menú de botones interactivos (`ReplyKeyboardMarkup`):

1. **Selección de Tienda**: botones directos para cada tienda (Éxito, Alkosto, Mercado Libre, Falabella, K-tronix, Olímpica, Carulla, Homecenter, Promocajita, Todo Colombia, Exterior, Mis Objetivos).
2. **Submenú de Categorías**: al tocar una tienda, se despliegan sus departamentos:
   - `🌟 TODO` (mejores rebajas generales de la tienda)
   - `📺 Smart TV`
   - `💻 Portátiles`
   - `🖥️ Monitores`
   - `📱 Celulares`
   - `👟 Zapatos y Tenis`
   - `🎧 Audio y Diademas`
   - `❄️ Electrodomésticos`
   - `👕 Ropa y Moda`
   - `⬅️ Volver a Tiendas`
3. **Respuesta inmediata y cancelación reactiva**: al presionar una opción, el bot notifica de inmediato (`🔍 Revisando ofertas de...`) y muestra el estado "escribiendo..." mientras consulta la tienda. Si tocas otra categoría mientras se estaban enviando resultados anteriores, la búsqueda previa se cancela y se atiende la nueva solicitud de inmediato sin trabas.

`/objetivos` (tus topes de precio) y `/estado` (alertas enviadas hoy) siguen
funcionando si los escribes, pero no aparecen en el menú para no llenarlo de
cosas que casi no se usan.

Aparecen solos en el menú de Telegram al escribir `/`, porque el bot los
registra con `setMyCommands` en cada corrida.

### Los comandos responden al instante

Telegram entrega cada mensaje al webhook del servicio (`POST /telegram`) apenas
lo escribes: no hay reloj de por medio. Lo que tarda es la consulta en sí —
preguntarle a las tiendas y mandar una tarjeta cada 3,5 segundos.

Antes esto colgaba de un cron de GitHub Actions cada 5 minutos, y no funcionó:
el `schedule` de GitHub es *best-effort* y en un repo público gratuito
sencillamente no dispara. En las primeras 8 horas del proyecto, un cron `*/5`
que debía correr ~93 veces corrió **una**. Por eso los comandos se quedaban sin
respuesta, y por eso ahora todo vive en Render.

> Telegram **no permite webhook y `getUpdates` a la vez**. Con el webhook
> registrado, `python radar.py --comandos` deja de ver mensajes; para volver a
> usarlo desde el portátil hay que quitar el webhook primero
> (`telegram.quitar_webhook()`).

### Pedir dos veces no repite lo mismo

Cada comando prioriza lo que **no** te ha mostrado, cruzando dos memorias: lo
que alertó el radar programado y lo que ya se envió respondiendo comandos. Si
se acaban las novedades, completa con las mejores y las marca como
*"ya te la había mostrado"*.

### Por qué los comandos no tocan el historial

Una consulta a voluntad **no marca nada como avisado**. Si `/alkosto` archivara
lo que muestra, el radar programado dejaría de avisarte de esas mismas ofertas
después. Por eso [comandos.py](core/comandos.py) guarda su punto de lectura en
un archivo aparte y el flujo de comandos nunca escribe `radar.db`.


---

## Un producto, una alerta

El mismo televisor está en las cinco tiendas, muchas veces al precio exacto.
Verlo cinco veces no es informar, es ruido. Dos reglas lo evitan:

**El revendedor solo entra si mejora el precio.** Si el marketplace del Éxito
cobra lo mismo que Alkosto por el mismo producto, sobra: se prefiere la tienda
original. Solo aparece cuando cobra menos.

**Se agrupa por precio y señas, no por título.** Comparar títulos no funciona
porque cada tienda escribe distinto:

```
Alkosto:  TV KALLEY 50" Pulgadas 126 cm 50G315 4K-UHD MAX LED Smart TV Google
Éxito:    Televisor Kalley 50G315a 50 Pulgadas 4Kuhd Max Smart Tv
```

Lo que sí coincide es el precio exacto y un par de señas propias (marca,
modelo), descartando las palabras que aparecen en medio catálogo —*televisor*,
*pulgadas*, *smart*, *4K*— y los colores. Cuando el mismo producto está en
varias tiendas, se manda una sola tarjeta con la nota *"También en K-tronix"*.

Medido sobre el catálogo real: **de 1.743 candidatas quedan 775**. Más de la
mitad de lo que iba a llegarte eran repeticiones.


---

## Cómo se leen las alertas

| Ícono | Significado |
|---|---|
| 🚨 | Posible error de precio — verifica antes de pagar, suelen cancelarse |
| 🔥 | Caída confirmada contra el historial propio del bot |
| 🏷 | Oferta que superó el umbral |
| ⏳ | El precio vence pronto: relámpago o trasnochón |
| 📉 | Precio mínimo real de los últimos 30 días |
| ⚠ | La rebaja no mejora ese mínimo |
| 📋 | Resumen agrupado |
| 💳 | Requiere una tarjeta o banco específico para ese precio |
| 🎟 | Cupón — tócalo para copiarlo |
| 🇨🇴 | Costo estimado puesto en Colombia |
| 📌 | Por qué se avisó (objetivo cumplido, mínimo histórico, rebaja + cupón…) |

---

## Estructura

```
radar.py            Orquestador: recolecta, decide, envía
server.py           Servidor de Render: rondas, ping y webhook de comandos
render.yaml         Blueprint de despliegue
watchlist.json      Qué buscar y con qué objetivos de precio
config.py           Variables de entorno y umbrales
core/
  http.py           Cliente HTTP con reintentos (librería estándar)
  models.py         Modelo único de oferta
  coupons.py        Extracción de cupones por expresiones regulares
  objetivos.py      Objetivos de precio y filtrado de accesorios
  filtros.py        Listas de intereses y de veto por palabra
  comandos.py       Comandos de Telegram y su punto de lectura
  veracidad.py      Detecta el inflado previo y calcula el mínimo real
  scoring.py        Decide qué se alerta, con qué confianza y con qué urgencia
  store.py          SQLite: deduplicación e historial de precios
  respaldo.py       Copia del historial en GitHub (Render borra el disco)
  landed.py         Costo puesto en Colombia (flete, IVA, arancel)
  fx.py             TRM oficial con respaldos
  telegram.py       Formato y envío de mensajes
sources/
  slickdeals.py     RSS de la comunidad (EE. UU.)
  algolia_co.py     Alkosto, K-tronix (Algolia)
  vtex.py           Éxito, Carulla, Olímpica, Droguerías (VTEX)
  falabella.py      Falabella, Homecenter (Next.js SSR)
  mercadolibre.py   Centro oficial de liquidaciones y cupones (SSR)
  promocajita.py    Canal público de Telegram de ofertas
  ebay.py           Browse API (opcional)
pruebas.py          Pruebas de la lógica de decisión y catálogo
```

## Comandos

```bash
python radar.py --dry-run
```

```bash
python radar.py --source algolia_co
```

```bash
python radar.py --chat-id
```

```bash
python radar.py --test-telegram
```

```bash
python pruebas.py
```

```bash
python server.py
```

---

## Notas y límites

- **eBay es opcional.** Sin `EBAY_CLIENT_ID` y `EBAY_CLIENT_SECRET` esa fuente se
  omite sola. Verifica los nombres de vendedor en `watchlist.json`: los outlets
  oficiales cambian de nombre.
- **Alkosto/K-tronix** usan la llave pública de solo lectura que su propia web
  entrega al navegador. Si dejan de responder, es que la rotaron: búscala en el
  HTML de la página de búsqueda y ponla en `ALGOLIA_APP_ID` y `ALGOLIA_API_KEY`.
- **No hay detección de envío gratis.** El único campo relacionado que exponen
  (`hasKasadoNotFree_boolean`) tiene significado ambiguo, y una etiqueta de
  "envío gratis" equivocada es peor que no tenerla.
- **El costo puesto en Colombia es una estimación.** El flete real depende de tu
  casillero y del peso volumétrico; el arancel varía por partida arancelaria.
- **La primera semana el bot es menos preciso** porque aún no tiene historial
  propio. Entre más rondas acumula, mejor distingue una ganga de un precio inflado.
