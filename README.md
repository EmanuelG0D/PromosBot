# Radar de Ofertas — Colombia & USA (PromosBot)

Motor autónomo de rastreo, verificación, branding gráfico y publicación multicanal de ofertas de alta confianza para **Colombia y Estados Unidos**. Avisa automáticamente por **Telegram** y publica en la Página oficial e **Historias de Facebook**. Costo de infraestructura en producción: **$0** (Render Free Tier + GitHub).

- **Sin dependencias pesadas innecesarias**: lógica construida en Python 3.10+, con procesamiento gráfico en Pillow (`PIL`).
- **Arquitectura robusta y resiliente**: consume APIs directas, SSR y feeds optimizados sin scrapers frágiles de CSS.
- **Motor de Branding Gráfico (`core/branding.py`)**: genera automáticamente tarjetas visuales premium 1080x1080 para Telegram/Feed y 1080x1920 (9:16) para Historias de Facebook, con marcos dinámicos y **logotipos oficiales de las tiendas integrados localmente en Render**.
- **Detector de rebajas falsas e inflado**: memoria histórica propia en SQLite (`radar.db`) para medir la veracidad frente al mínimo real de 30 días.
- **Respaldo automático en GitHub**: sincronización bidireccional de la base de datos para sobrevivir a los reinicios del plan gratuito de Render.

---

## 1. Fuentes que vigila el Radar

| Fuente | Tipo de Catálogo | Método de Extracción | Logotipo Oficial Local |
|---|---|---|:---:|
| **Amazon** | Electrónica, calzado, gaming y hogar con envío a Colombia | PromoHunter / Slickdeals / ASIN directo | ✅ `amazon.png` |
| **Falabella** | Tecnología, moda, zapatillas y electrohogar | Next.js SSR JSON (`__NEXT_DATA__`) | ✅ `falabella.png` |
| **Homecenter** | Ferretería, herramientas, muebles y hogar | Catálogo Next.js integrado | ✅ `homecenter.png` |
| **Alkosto** | Tecnología, televisores, celulares y línea blanca | Algolia Search Index (`alkostoIndexAlgoliaPRD`) | ✅ `alkosto.png` |
| **K-tronix** | Consolas, computadores, audio y videojuegos | Algolia Search Index (`ktronixIndexAlgoliaPRD`) | ✅ `ktronix.png` |
| **Alkomprar** | Electrodomésticos y tecnología para el hogar | Algolia Search Index | ✅ `alkomprar.png` |
| **Éxito** | Gran superficie, tecnología y supermercado | VTEX Catalog API (ordenado por mayor descuento) | ✅ `exito.png` |
| **Carulla** | Variedades y productos selectos | VTEX Catalog API | ✅ `carulla.png` |
| **Olímpica** | Supermercado y tecnología nacional | VTEX Catalog API | ✅ `olimpica.png` |
| **Jumbo Colombia** | Electrónica y catálogo nacional | VTEX Catalog API | ✅ `jumbo.png` |
| **Mercado Libre Colombia** | Liquidaciones oficiales del día y cupones | Hub oficial de ofertas SSR (`_n.ctx.r`) | ✅ `mercadolibre.png` |
| **PROMOCAJITA** | Comunidad colombiana de ofertas y cupones | API / S3 JSON estructurado | ✅ `promocajita.png` |
| **Slickdeals** | Ofertas en EE. UU. (Nike, Adidas, Puma, Amazon, eBay) | RSS comunitario + Detección de importación 🇺🇸 | ✅ Logos de marca |
| **Nike / Adidas / Puma** | Calzado y moda deportiva | VTEX / Slickdeals / Feeds oficiales | ✅ Logos de marca |
| **Koaj** | Moda y calzado colombiano | Scraper nativo en `sources/koaj.py` | ✅ `koaj.png` |
| **Totto / Arturo Calle** | Ropa, morrales y accesorios | Tiendas oficiales VTEX | ✅ Logos de marca |
| **Imusa / Haceb / Oster** | Electrohogar y cocina | Tiendas oficiales VTEX | ✅ Logos de marca |
| **eBay** *(opcional)* | Outlets certificados y tecnología | Browse API oficial de eBay | ✅ `ebay.png` |

---

## 2. Motor Gráfico de Marca (Branding Visual)

A diferencia de bots convencionales que comparten capturas genéricas o enlaces planos, este bot genera automáticamente **tarjetas gráficas personalizadas de marca** (`core/branding.py`) listas para cautivar visualmente a la audiencia:

```
┌────────────────────────────────────────────────────────┐
│  ▲ POR SÓLO ★                               [ FALABELLA ] │
│                                                        │
│                    [ FOTO LIMPIA ]                     │
│                  DEL PRODUCTO EN HD                    │
│                                                        │
│   RADAR                                                │
│   PROMOS                        COP $792.900           │
│  ────────                       *ENVÍO GRATIS DIRECTO  │
└────────────────────────────────────────────────────────┘
```

### Características del Sistema de Branding:
1. **Lienzo Cuadrado 1080x1080 (HD):** Resolución estándar para publicaciones en canales de Telegram y muro de Facebook.
2. **Rotación Dinámica de Color:** Cada oferta rota de forma determinista entre 3 paletas neón:
   - 🟢 **Verde Esmeralda Neón:** Acento fresco y tecnológico.
   - 🔵 **Azul Eléctrico / Cyan:** Acento corporativo y moderno.
   - 🟠 **Naranja Fuego:** Acento de urgencia y liquidación.
3. **Cápsula de Tienda (Doble Blindaje):**
   - Ubicada en la esquina superior derecha (`x=790, y=28`).
   - Fondo blanco puro con bordes redondeados y contorno del color de acento.
   - **Para Telegram:** Logotipos oficiales locales en `assets/logos/` con transparencia RGBA.
   - **Para Facebook (Blindaje Anti-Falsificación):** Muestra el nombre de la tienda en **texto tipográfico limpio** (`FALABELLA`, `AMAZON`, `ÉXITO`, `ALKOSTO`), evitando logotipos vectoriales de marcas registradas que activan los filtros automatizados de propiedad intelectual de Meta.
4. **Respeto Estricto de Moneda:** Si la oferta es de Colombia muestra `COP $...`, y si proviene de EE. UU. (Slickdeals, Amazon Global) muestra `US$ ...` sin conversiones forzadas engañosas.
5. **Detección de Importación 🇺🇸:** Las ofertas internacionales de Slickdeals (como calzado o gadgets en USA) son etiquetadas con la bandera de Estados Unidos y la nota de importación.

---

## 3. Publicación Multicanal Automática

### A. Telegram (Grupo, Canal y Consultas Privadas)
- **Tarjeta por Oferta:** Cada oferta se despacha con la foto del producto enmarcada con el branding del bot, título depurado, precio tachado, porcentaje de descuento y cupón copiable con un toque.
- **Teclado Interactivo (`/menu`):** Menú de navegación por tiendas y categorías (`Smart TV`, `Portátiles`, `Celulares`, `Zapatos y Tenis`, `Audio`, etc.) con cancelación reactiva en tiempo real.
- **Auto-aprobación de Usuarios:** Acceso instantáneo para nuevos miembros con invitación automática al canal oficial (`@RadarPromoCol`).

### B. Facebook (Página Oficial en Carrusel e Historias 9:16)
- **Modo Carrusel de Alto Engagement (2 a 4 ofertas cada 45 min):**
  - **Motor Spintax de 3 Niveles:** Combina de forma aleatoria un titular llamativo, una bajada editorial y un llamado a la acción interactivo (+512 combinaciones únicas) para evitar duplicados y shadowban.
  - **Cero Enlaces en el Feed:** El mensaje principal del muro no contiene URLs salientes, protegiendo el alcance algorítmico de la página.
  - **Fichas Descriptivas por Foto (`render_caption_foto`):** Al tocar o deslizar cualquier foto en Facebook, el panel lateral muestra el título del producto, tienda, precio, descuento, cupón y enlace cloaked seguro (`https://promosbot.onrender.com/ir/{hash}`).
  - **Blindaje Anti-Falsificaciones:** En lugar de imágenes de logotipos comerciales, estampa el nombre de la tienda en texto plano para eludir los filtros de derechos de marca de Meta.
- **Regla del Camuflaje (5 a 1):** Cada 5 publicaciones comerciales, el bot publica automáticamente un post orgánico de interacción/comunidad sin enlaces para mantener una reputación óptima con el algoritmo.
- **Historias de Facebook (Stories 1080x1920):**
  - Adapta la tarjeta al formato vertical 9:16 con fondo degradado y marco superior/inferior.
  - Genera stickers interactivos nativos (`story_link` y `story_text`) para dirigir tráfico directo hacia la oferta.

---

## 4. Estructura del Proyecto

```text
BotPromos/
├── radar.py                 # Orquestador: recolecta, filtra, califica y envía
├── server.py                # Servidor Webhook de Render: cron interno, webhook y /healthz
├── render.yaml              # Especificación de despliegue en infraestructura Render
├── watchlist.json           # Intereses, marcas deseadas y objetivos de precio
├── config.py                # Variables de entorno y umbrales de configuración
├── assets/
│   └── logos/               # Logotipos oficiales locales (Render / Offline)
│       ├── amazon.png
│       ├── falabella.png
│       ├── alkosto.png
│       ├── exito.png
│       ├── mercadolibre.png
│       ├── promocajita.png
│       └── ...
├── core/
│   ├── branding.py          # Motor de composición visual (Pillow 1080x1080)
│   ├── facebook.py          # Publicación en Feed e Historias vía Graph API
│   ├── telegram.py          # Formato de mensajes, envío multipart y webhooks
│   ├── comandos.py          # Lógica de comandos interactivos y menú de tiendas
│   ├── models.py            # Esquema único Deal
│   ├── scoring.py           # Algoritmo de puntuación, confianza y urgencia
│   ├── veracidad.py         # Historial de precios y filtro de inflado previo
│   ├── store.py             # Base de datos SQLite local (deduplicación e historial)
│   ├── respaldo.py          # Sincronización de radar.db con GitHub API
│   ├── landed.py            # Cálculo de arancel, IVA y flete de casillero (USA -> CO)
│   ├── fx.py                # Consulta de la TRM oficial del día
│   ├── whitelist.py         # Control de acceso y lista de permitidos
│   └── http.py              # Cliente HTTP con reintentos y timeouts estrictos
├── sources/
│   ├── algolia_co.py        # Alkosto, K-tronix, Alkomprar
│   ├── vtex.py              # Éxito, Carulla, Olímpica, Jumbo, Nike, Totto, etc.
│   ├── falabella.py         # Falabella y Homecenter
│   ├── mercadolibre.py      # Centro de ofertas de Mercado Libre Colombia
│   ├── promocajita.py       # Scraper estructurado de Promocajita
│   ├── promohunter.py       # Ofertas verificadas con envío gratis a Colombia
│   ├── slickdeals.py        # Ofertas comunitarias de EE. UU. (Puma, Nike, Apple)
│   ├── koaj.py              # Catálogo de moda Koaj Colombia
│   └── ebay.py              # eBay Browse API
└── pruebas.py               # Suite de 233 pruebas unitarias automatizadas
```

---

## 5. Control Antispam y Memoria Histórica

1. **Memoria de Precios (Mínimo de 30 Días):**
   - El bot registra cada precio observado en `radar.db`.
   - Si una tienda infla el precio de $300.000 a $500.000 para luego "rebajarlo" a $460.000, el bot detecta que no supera el mínimo histórico y **silencia la alerta** para evitar engaños.
2. **Deduplicación Inteligente:**
   - Una oferta ya alertada no se repite a menos que baje al menos un 10% adicional o hayan pasado 14 días.
3. **Límite Diario (`MAX_ALERTS_PER_DAY`):**
   - Tope máximo de 40 alertas diarias para mantener el canal con calidad y sin saturar al usuario.
4. **Agrupación de Productos Idénticos:**
   - Si el mismo televisor o portátil se vende en Éxito, Alkosto y Falabella, se emite una sola alerta priorizando el mejor precio e indicando *"También disponible en..."*.

---

## 6. Variables de Entorno (`.env`)

Copia `.env.example` a `.env` y configura tus credenciales:

```bash
# Telegram
TELEGRAM_BOT_TOKEN="123456789:ABCdefGhIJKlmNoPQRstuVWXyz"
TELEGRAM_CHAT_ID="-1001234567890"       # Canal o supergrupo donde se publican las alertas
TELEGRAM_ADMIN_ID="5583002220"           # Chat ID personal del administrador

# Facebook (Opcional - para publicación automática)
FB_PAGE_ID="123456789012345"
FB_PAGE_ACCESS_TOKEN="EAA..."

# Respaldo en la Nube (para Render Free Tier)
GITHUB_TOKEN="ghp_..."                   # Token de GitHub con permiso repo:contents
GITHUB_REPO="usuario/PromosBot"

# Parámetros del Radar
RUN_TOKEN="tu_palabra_secreta"
MIN_DISCOUNT_PCT="40"
INSTANT_DISCOUNT_PCT="60"
MAX_ALERTS_PER_RUN="8"
MAX_ALERTS_PER_DAY="40"
```

---

## 7. Ejecución Local y Pruebas

```bash
# Ejecutar una ronda de prueba en consola sin enviar a Telegram
python radar.py --dry-run

# Probar la conexión con el bot de Telegram
python radar.py --test-telegram

# Ejecutar la suite completa de 233 pruebas unitarias
python pruebas.py

# Iniciar el servidor web local con soporte de webhook
python server.py
```

---

## 8. Despliegue en Render (Paso a Paso)

1. **Crear Blueprint:** En el panel de Render, selecciona *New → Blueprint* y conecta este repositorio.
2. **Configurar Variables:** En la pestaña *Environment* del servicio Web, añade las variables de Telegram y GitHub mencionadas arriba.
3. **Ping de Mantenimiento:** Configura un monitor gratuito (como [cron-job.org](https://cron-job.org) o UptimeRobot) que haga ping cada 5 minutos al endpoint de salud:
   ```text
   https://tu-servicio.onrender.com/healthz
   ```
   *(Recomendado: de 6:00 a.m. a medianoche hora de Colombia para no exceder las 750 horas mensuales del plan gratuito).*
4. **¡Listo!** El bot operará 24/7 sin costo, sincronizando su historial con GitHub y sirviendo los logos oficiales directamente desde Render.
