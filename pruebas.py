"""Pruebas rapidas del radar: python pruebas.py

No tocan la red: validan la logica que decide que se alerta y que no.
"""
from __future__ import annotations

import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parent

import config
from core import filtros, objetivos, veracidad
from core.coupons import extract_coupons, needs_clipping
from core.landed import calcular
from core.models import Deal
from core.scoring import evaluar
from core.store import Store


def oferta(**kwargs) -> Deal:
    base = dict(
        source="vtex", store="Exito", country="CO", key="k1",
        title="Producto de prueba", url="https://ejemplo.co/p",
        price=100_000.0, currency="COP", list_price=400_000.0,
    )
    base.update(kwargs)
    return Deal(**base)


class PruebaCupones(unittest.TestCase):
    def test_extrae_codigos_tipicos(self):
        self.assertEqual(extract_coupons("Nike Shoes $24 w/ code SAVE20"), ["SAVE20"])
        self.assertEqual(extract_coupons("Use promo code: BLACK40 at checkout"), ["BLACK40"])
        self.assertEqual(extract_coupons("Aplica el cupon VERANO25"), ["VERANO25"])

    def test_ignora_texto_corriente(self):
        self.assertEqual(extract_coupons("Great shoes with code quality stitching"), [])
        self.assertEqual(extract_coupons("Sin cupones aqui"), [])

    def test_detecta_cupon_de_activacion(self):
        self.assertTrue(needs_clipping("Clip the 20% coupon on the product page"))
        self.assertFalse(needs_clipping("No requiere nada"))


class PruebaCostoPuesto(unittest.TestCase):
    def test_bajo_200_queda_exento(self):
        r = calcular(150.0, trm=4000.0, peso_lb=2.0)
        self.assertTrue(r.exento)
        self.assertEqual(r.iva_usd, 0.0)
        self.assertEqual(r.total_usd, 150.0 + r.flete_usd)

    def test_sobre_200_liquida_iva_y_arancel(self):
        r = calcular(250.0, trm=4000.0, peso_lb=2.0)
        self.assertFalse(r.exento)
        self.assertGreater(r.iva_usd, 0)
        self.assertGreater(r.total_cop, 250.0 * 4000.0)

    def test_convierte_a_pesos(self):
        r = calcular(100.0, trm=4000.0, peso_lb=1.0)
        self.assertEqual(r.total_cop, round(r.total_usd * 4000.0))


class PruebaDecision(unittest.TestCase):
    sin_historial = (0, None, None)

    def test_marketplace_con_precio_inflado_no_es_glitch(self):
        # Caso real: portatil "de 36.799.990" vendido en 1.679.990 por un tercero.
        d = oferta(price=1_679_990.0, list_price=36_799_990.0,
                   marketplace=True, list_price_trusted=False)
        v = evaluar(d, self.sin_historial)
        self.assertFalse(v.glitch)
        self.assertFalse(v.alertar)

    def test_descuento_real_de_tienda_propia_si_alerta(self):
        d = oferta(price=959_900.0, list_price=3_699_900.0)
        v = evaluar(d, self.sin_historial)
        self.assertTrue(v.alertar)
        self.assertFalse(v.glitch)  # 74% queda bajo el umbral de 75%
        self.assertGreater(len(v.motivo), 0)

    def test_caida_contra_historial_propio_sube_la_confianza(self):
        d = oferta(price=100_000.0, list_price=120_000.0)
        v = evaluar(d, (5, 300_000.0, 250_000.0))
        self.assertTrue(v.alertar)
        self.assertEqual(v.confianza, "alta")

    def test_sin_stock_no_alerta(self):
        v = evaluar(oferta(in_stock=False), self.sin_historial)
        self.assertFalse(v.alertar)


class PruebaDeduplicacion(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "test.db")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_no_repite_la_misma_alerta(self):
        d = oferta()
        self.assertTrue(self.store.should_alert(d)[0])
        self.store.mark_alerted(d)
        self.assertFalse(self.store.should_alert(d)[0])

    def test_vuelve_a_avisar_si_baja_mas(self):
        d = oferta()
        self.store.mark_alerted(d)
        mas_barata = oferta(price=d.price * 0.8)
        self.assertTrue(self.store.should_alert(mas_barata)[0])

    def test_no_guarda_el_mismo_precio_dos_veces(self):
        for _ in range(5):
            self.store.record(oferta(price=300_000.0))
        n, _, _ = self.store.price_stats("k1")
        self.assertEqual(n, 1)

    def test_historial_de_precios(self):
        for precio in (300_000.0, 320_000.0, 310_000.0):
            self.store.record(oferta(price=precio))
        n, mediana, minimo = self.store.price_stats("k1")
        self.assertGreaterEqual(n, 1)
        self.assertIsNotNone(mediana)
        self.assertEqual(minimo, min(300_000.0, 310_000.0, 320_000.0))


class PruebaColapsoVariantes(unittest.TestCase):
    def test_colapsa_colores_y_tiendas_hermanas(self):
        from radar import _colapsar_variantes
        from core.scoring import Verdict
        v = Verdict(True)
        pares = [
            (oferta(key="a", store="Alkosto", title="Audifonos SONY WH-CH720N Negro", price=349900.0), v),
            (oferta(key="b", store="Alkosto", title="Audifonos SONY WH-CH720N Azul", price=349900.0), v),
            (oferta(key="c", store="K-tronix", title="Audifonos SONY WH-CH720N Negro", price=349900.0), v),
        ]
        unicas, hermanas = _colapsar_variantes(pares)
        self.assertEqual(len(unicas), 1)
        # Las dos variantes descartadas quedan asociadas al representante,
        # para marcarlas como avisadas y que no resurjan en la ronda siguiente.
        self.assertEqual(len(hermanas[unicas[0][0].key]), 2)
        self.assertIn("Tambien en K-tronix", unicas[0][0].notes)


class PruebaObjetivos(unittest.TestCase):
    metas = [
        {"termino": "freidora", "max_cop": 150000},
        {"termino": "sneakers", "max_usd": 35},
    ]

    def test_avisa_por_precio_absoluto_sin_importar_el_porcentaje(self):
        # El caso real: una freidora barata que solo figura como -45%.
        d = oferta(title="Freidora de Aire KALLEY 3.5 Litros Negro",
                   price=99_900.0, list_price=180_000.0)
        self.assertIsNotNone(objetivos.alcanzado(d, self.metas))

    def test_ignora_lo_que_sigue_caro(self):
        d = oferta(title="Freidora de Aire OSTER 6 Litros", price=389_030.0)
        self.assertIsNone(objetivos.alcanzado(d, self.metas))

    def test_descarta_accesorios(self):
        d = oferta(title="Canasta de repuesto para freidora de aire", price=39_900.0)
        self.assertIsNone(objetivos.alcanzado(d, self.metas))

    def test_respeta_la_moneda(self):
        # Un objetivo en dolares no debe dispararse con un precio en pesos.
        d = oferta(title="Sneakers Nike", price=120_000.0, currency="COP")
        self.assertIsNone(objetivos.alcanzado(d, self.metas))

    def test_los_terminos_entran_a_las_busquedas(self):
        self.assertEqual(objetivos.terminos_de_busqueda(self.metas),
                         ["freidora", "sneakers"])


class PruebaUrgencia(unittest.TestCase):
    sin_historial = (0, None, None)

    def test_objetivo_cumplido_interrumpe(self):
        d = oferta(title="Freidora KALLEY", price=99_900.0, list_price=180_000.0)
        v = evaluar(d, self.sin_historial, {"termino": "freidora", "max_cop": 150000})
        self.assertTrue(v.alertar)
        self.assertTrue(v.inmediata)

    def test_rebaja_mediana_va_al_resumen(self):
        d = oferta(price=100_000.0, list_price=180_000.0)   # -44%
        v = evaluar(d, self.sin_historial)
        self.assertTrue(v.alertar)
        self.assertFalse(v.inmediata)

    def test_descuentazo_interrumpe(self):
        d = oferta(price=100_000.0, list_price=300_000.0)   # -67%
        v = evaluar(d, self.sin_historial)
        self.assertTrue(v.inmediata)

    def test_rebaja_con_cupon_interrumpe(self):
        d = oferta(price=100_000.0, list_price=180_000.0, coupons=["AHORRA20"])
        v = evaluar(d, self.sin_historial)
        self.assertTrue(v.inmediata)
        self.assertIn("rebaja + cupon", v.etiquetas)


def historial(precios, dias=30):
    """Construye un historial repartido en una ventana de N dias."""
    inicio = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=dias)
    paso = dias / max(len(precios) - 1, 1)
    return [((inicio + dt.timedelta(days=i * paso)).isoformat(), float(p))
            for i, p in enumerate(precios)]


class PruebaVeracidad(unittest.TestCase):
    def test_detecta_la_maniobra_de_subir_y_luego_bajar(self):
        # Valia 300.000 por semanas, lo subieron a 500.000, y ahora lo
        # "rebajan" a 460.000 anunciando un descuentazo.
        v = veracidad.analizar(460_000.0, historial([300_000, 300_000, 500_000, 500_000]))
        self.assertTrue(v.es_falsa)
        self.assertEqual(v.minimo_ventana, 300_000.0)
        self.assertGreater(v.alza_previa, 60)
        self.assertIn("subieron el precio", v.detalle)

    def test_rebaja_de_verdad_pasa(self):
        v = veracidad.analizar(250_000.0, historial([300_000, 300_000, 310_000]))
        self.assertTrue(v.es_real)
        self.assertAlmostEqual(v.descuento_real, 16.7, places=1)

    def test_volver_al_precio_de_siempre_no_es_oferta(self):
        # Sube y regresa exactamente a donde estaba: descuento anunciado, cero real.
        v = veracidad.analizar(300_000.0, historial([300_000, 450_000, 450_000]))
        self.assertTrue(v.es_falsa)

    def test_sin_historial_no_opina(self):
        v = veracidad.analizar(250_000.0, historial([300_000]))
        self.assertEqual(v.veredicto, veracidad.SIN_DATOS)
        self.assertFalse(v.es_falsa)
        self.assertFalse(v.es_real)

    def test_historial_muy_corto_no_opina(self):
        v = veracidad.analizar(250_000.0, historial([300_000, 300_000, 300_000], dias=2))
        self.assertEqual(v.veredicto, veracidad.SIN_DATOS)


class PruebaVeracidadEnDecision(unittest.TestCase):
    sin_historial = (0, None, None)

    def test_la_rebaja_falsa_no_se_avisa(self):
        d = oferta(price=460_000.0, list_price=900_000.0)   # la tienda declara -49%
        v = veracidad.analizar(460_000.0, historial([300_000, 300_000, 500_000, 500_000]))
        veredicto = evaluar(d, self.sin_historial, None, v)
        self.assertFalse(veredicto.alertar)
        self.assertIn("rebaja falsa", veredicto.motivo)

    def test_la_rebaja_real_interrumpe(self):
        d = oferta(price=250_000.0, list_price=320_000.0)   # solo -22% declarado
        v = veracidad.analizar(250_000.0, historial([300_000, 300_000, 310_000]))
        veredicto = evaluar(d, self.sin_historial, None, v)
        self.assertTrue(veredicto.alertar)
        self.assertTrue(veredicto.inmediata)
        self.assertEqual(veredicto.confianza, "alta")

    def test_un_objetivo_de_precio_manda_sobre_la_sospecha(self):
        # Si el precio absoluto es el que yo queria, me interesa igual.
        d = oferta(title="Freidora KALLEY", price=140_000.0, list_price=400_000.0)
        v = veracidad.analizar(140_000.0, historial([120_000, 200_000, 200_000]))
        self.assertTrue(v.es_falsa)
        veredicto = evaluar(d, self.sin_historial,
                            {"termino": "freidora", "max_cop": 150000}, v)
        self.assertTrue(veredicto.alertar)
        self.assertTrue(veredicto.inmediata)


class PruebaRelampago(unittest.TestCase):
    sin_historial = (0, None, None)

    def vence_en(self, horas):
        return (dt.datetime.now(dt.timezone.utc)
                + dt.timedelta(hours=horas)).isoformat().replace("+00:00", "Z")

    def test_un_trasnochon_moderado_si_alerta(self):
        # -25% no pasaria el umbral normal, pero se vence en 3 horas.
        d = oferta(price=750_000.0, list_price=1_000_000.0, expires_at=self.vence_en(3))
        v = evaluar(d, self.sin_historial)
        self.assertTrue(v.alertar)
        self.assertTrue(v.inmediata)
        self.assertIn("termina en", v.motivo)

    def test_sin_vencimiento_el_mismo_descuento_no_pasa(self):
        d = oferta(price=750_000.0, list_price=1_000_000.0)
        self.assertFalse(evaluar(d, self.sin_historial).alertar)

    def test_vencimiento_lejano_no_es_relampago(self):
        d = oferta(price=750_000.0, list_price=1_000_000.0, expires_at=self.vence_en(24 * 30))
        self.assertFalse(evaluar(d, self.sin_historial).alertar)


class PruebaTiendaSlickdeals(unittest.TestCase):
    def test_usa_el_sufijo_at_tienda(self):
        from sources.slickdeals import _tienda
        self.assertIn("Woot", _tienda("Fragrance Testers and Gift Sets!  at Woot!"))

    def test_usa_la_marca_inicial(self):
        from sources.slickdeals import _tienda
        self.assertIn("Nike", _tienda("Nike Men's Shox NZ Shoes $62.87 + Free Shipping"))

    def test_no_inventa_tienda_por_menciones_sueltas(self):
        # Antes atribuia esta oferta a Target solo por nombrarla en la lista.
        from sources.slickdeals import _tienda
        titulo = "10% Off Select Gift Cards: Starbucks, Target, Nintendo, adidas & More"
        self.assertEqual(_tienda(titulo), "Slickdeals")


class PruebaPieDeFoto(unittest.TestCase):
    def test_respeta_el_limite_de_telegram(self):
        from core.telegram import _pie_de_foto
        from core.scoring import Verdict
        d = oferta(title="X" * 200, currency="COP")
        d.notes = ["nota larguisima " * 20] * 3
        v = Verdict(True, "motivo", ["etiqueta " * 30] * 4)
        pie = _pie_de_foto(d, v)
        self.assertLessEqual(len(pie), 1024)

    def test_conserva_el_enlace(self):
        from core.telegram import _pie_de_foto
        from core.scoring import Verdict
        d = oferta(title="Producto", url="https://tienda.co/oferta")
        pie = _pie_de_foto(d, Verdict(True))
        self.assertIn("https://tienda.co/oferta", pie)


class PruebaFiltros(unittest.TestCase):
    intereses = ["adidas", "ssd", "nintendo", "air fryer"]
    vetadas = ["perfume", "fragrance", "gift card", "vitamin"]

    def test_veta_lo_que_no_interesa(self):
        self.assertTrue(filtros.descartado("Fragrance Gift Sets at Woot", self.vetadas))
        self.assertTrue(filtros.descartado("10% Off Select Gift Cards", self.vetadas))
        self.assertFalse(filtros.descartado("adidas Runfalcon 5 Shoes $21", self.vetadas))

    def test_exige_que_mencione_un_interes(self):
        self.assertTrue(filtros.pertinente("Nintendo Switch OLED $299", self.intereses))
        self.assertTrue(filtros.pertinente("Samsung 2TB SSD $89", self.intereses))
        self.assertFalse(filtros.pertinente("Boho Maxi Dress w/ Pockets", self.intereses))

    def test_sin_lista_deja_pasar_todo(self):
        self.assertTrue(filtros.pertinente("cualquier cosa", []))
        self.assertFalse(filtros.descartado("cualquier cosa", []))

    def test_ignora_tildes_y_mayusculas(self):
        self.assertTrue(filtros.pertinente("NINTENDO Switch", ["nintendo"]))
        self.assertTrue(filtros.descartado("Perfúme importado", ["perfume"]))


class PruebaTopeDiario(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "t.db")

    def tearDown(self):
        self.store.close(); self.tmp.cleanup()

    def test_cuenta_los_envios_del_dia(self):
        self.assertEqual(self.store.enviadas_hoy(), 0)
        for _ in range(3):
            self.store.sumar_enviada()
        self.assertEqual(self.store.enviadas_hoy(), 3)


class PruebaConsolasSinJuegos(unittest.TestCase):
    """Consolas y accesorios si, videojuegos no.

    La senal que los separa: el hardware se nombra ("Console", "System",
    "Controller", "Case for"), mientras que un juego solo menciona la
    plataforma entre parentesis. Estos casos salieron de resultados reales.
    """

    @classmethod
    def setUpClass(cls):
        cfg = json.loads((BASE / "watchlist.json").read_text(encoding="utf-8"))
        cls.cfg = cfg["slickdeals"]

    def pasa(self, titulo):
        if filtros.descartado(titulo, self.cfg["excluir"]):
            return False
        return filtros.pertinente(titulo, self.cfg["incluir"])

    def test_los_juegos_no_pasan(self):
        for titulo in ("Biomutant (Nintendo Switch) at Amazon $13.99",
                       "Elden Ring (Xbox Series X) $29.99",
                       "R-TYPE HD+ (Nintendo Switch) at Amazon $29.23",
                       "Borderlands 4 Super Deluxe Edition (Xbox Series X) $49.99",
                       "Black Book (Nintendo Switch Digital Download) $4.99"):
            self.assertFalse(self.pasa(titulo), f"deberia vetarse: {titulo}")

    def test_el_hardware_si_pasa(self):
        for titulo in ("Nintendo Switch 2 System Black at Woot! $449.99",
                       "Playstation 5 Console (Disc) - 1TB $449",
                       "Xbox Elite Wireless Controller Series 2 $119",
                       "Orzly Carrying Case for Nintendo Switch 2 $12",
                       "PowerA PS Portal Charging Station (Refurb) $11.99"):
            self.assertTrue(self.pasa(titulo), f"deberia pasar: {titulo}")

    def test_sigue_vetando_lo_que_no_es_tecnologia_ni_hogar(self):
        for titulo in ("Fragrance Testers and Gift Sets at Woot",
                       '17" PUMA Pitch Ball Backpack $16.04',
                       "6-Pk PUMA Low-Cut Logo Runner Socks $5.26"):
            self.assertFalse(self.pasa(titulo), f"deberia vetarse: {titulo}")


class PruebaEnlacesVtex(unittest.TestCase):
    """Los enlaces deben usar el dominio publico, no el interno de la API.

    tienda.exito.com y secure.carulla.com devuelven la ficha, pero con un
    redirect en JavaScript a su portada: el usuario abre la oferta y termina
    en el inicio del sitio sin ver el producto.
    """

    def test_ignora_el_dominio_interno_de_la_api(self):
        from sources.vtex import TIENDAS, enlace_publico
        producto = {"linkText": "televisor-tcl-p8k-124219799",
                    "link": "https://tienda.exito.com/televisor-tcl-p8k-124219799/p"}
        url = enlace_publico(TIENDAS["exito"], producto)
        self.assertTrue(url.startswith("https://www.exito.com/"), url)
        self.assertNotIn("tienda.exito.com", url)

    def test_carulla_tampoco_usa_secure(self):
        from sources.vtex import TIENDAS, enlace_publico
        producto = {"linkText": "televisor-abc-100021092-mp",
                    "link": "https://secure.carulla.com/televisor-abc-100021092-mp/p"}
        url = enlace_publico(TIENDAS["carulla"], producto)
        self.assertTrue(url.startswith("https://www.carulla.com/"), url)

    def test_cae_al_link_de_la_api_si_no_hay_ruta(self):
        from sources.vtex import TIENDAS, enlace_publico
        producto = {"link": "https://www.exito.com/algo/p"}
        self.assertEqual(enlace_publico(TIENDAS["exito"], producto),
                         "https://www.exito.com/algo/p")


class PruebaMediosPagoAlkosto(unittest.TestCase):
    def test_traduce_las_etiquetas_utiles(self):
        from sources.algolia_co import _medios_pago
        hit = {"subcat-mediospago_string_mv": [
            "marcas", "BI_ELHO_ALKOS", "ofertas-davivienda-0-interes",
            "envios-rapidos-express", "recien-casados"]}
        etiquetas = _medios_pago(hit)
        self.assertIn("0% interes con Davivienda", etiquetas)
        self.assertIn("Envio rapido express", etiquetas)

    def test_descarta_las_etiquetas_internas(self):
        from sources.algolia_co import _medios_pago
        hit = {"subcat-mediospago_string_mv": ["BI_TVVI_ALKOS", "temporadas", "marcas"]}
        self.assertEqual(_medios_pago(hit), [])

    def test_el_aviso_generico_va_de_ultimo(self):
        from sources.algolia_co import _medios_pago
        hit = {"subcat-mediospago_string_mv": [
            "ofertas-medios-pago", "ofertas-davivienda-0-interes"]}
        self.assertEqual(_medios_pago(hit)[-1], "Descuento con medios de pago")


class PruebaComandos(unittest.TestCase):
    def test_el_catalogo_cubre_las_tiendas_anunciadas(self):
        from core.comandos import CATALOGO, MENU
        for comando in ("alkosto", "ktronix", "exito", "carulla", "olimpica", "exterior"):
            self.assertIn(comando, CATALOGO)
        # Todo lo que se anuncia en el menu debe existir como comando real.
        manejados = set(CATALOGO) | {"objetivos", "estado", "ayuda"}
        for comando, _descripcion in MENU:
            self.assertIn(comando, manejados, f"el menu ofrece /{comando} sin implementar")

    def test_cada_comando_apunta_a_una_fuente_valida(self):
        from core.comandos import CATALOGO
        from radar import FUENTES
        for comando, (fuente, _tiendas, titulo) in CATALOGO.items():
            # "*" es el comodin de /todo: mezcla todas las fuentes.
            self.assertIn(fuente, set(FUENTES) | {"*"},
                          f"/{comando} apunta a una fuente inexistente")
            self.assertTrue(titulo)

    def test_las_tiendas_existen_en_su_fuente(self):
        from core.comandos import CATALOGO
        from sources import algolia_co, vtex
        disponibles = {"algolia_co": set(algolia_co.TIENDAS), "vtex": set(vtex.TIENDAS)}
        for comando, (fuente, tiendas, _t) in CATALOGO.items():
            if not tiendas:
                continue
            for tienda in tiendas:
                self.assertIn(tienda, disponibles[fuente], f"/{comando}: {tienda} no existe")

    def test_exterior_no_exige_porcentaje_de_descuento(self):
        """Slickdeals no publica precio de lista: exigir descuento lo vaciaba."""
        import inspect
        import radar
        codigo = inspect.getsource(radar._mejores)
        self.assertIn('fuentes == ["slickdeals"]', codigo)

    def test_prioriza_lo_que_no_has_visto(self):
        """Pedir la misma tienda dos veces debe traer cosas distintas."""
        import inspect
        import radar
        codigo = inspect.getsource(radar._mejores)
        self.assertIn("nuevas + repetidas", codigo)

    def test_recuerda_lo_que_ya_mostro(self):
        import json as _json
        import tempfile
        from pathlib import Path as _Path
        from core import comandos
        with tempfile.TemporaryDirectory() as tmp:
            original = comandos.ESTADO
            comandos.ESTADO = _Path(tmp) / "estado.json"
            try:
                self.assertEqual(comandos.ya_mostradas(), set())
                comandos.marcar_mostradas(["a", "b"])
                self.assertEqual(comandos.ya_mostradas(), {"a", "b"})
                comandos.marcar_mostradas(["c"])
                self.assertEqual(comandos.ya_mostradas(), {"a", "b", "c"})
                # El offset de Telegram no se pierde al guardar lo mostrado.
                comandos._guardar_estado(99)
                comandos.marcar_mostradas(["d"])
                self.assertEqual(comandos._leer_estado(), 99)
            finally:
                comandos.ESTADO = original

    def test_solo_obedece_al_chat_configurado(self):
        """Sin este filtro, un extrano que encuentre el bot podria usarlo."""
        import inspect
        from core import comandos
        codigo = inspect.getsource(comandos.pendientes)
        self.assertIn("config.TELEGRAM_CHAT_ID", codigo)
        self.assertIn("ignorado", codigo)


class PruebaSeguridadYErrores(unittest.TestCase):
    def test_el_token_nunca_aparece_en_un_error(self):
        """El token viaja en la URL de Telegram; en un repo publico no puede
        terminar en los logs."""
        from core.http import url_segura
        sucia = "https://api.telegram.org/bot8489029941:AAEE6XvWabcdefghijklmnop/sendMessage"
        limpia = url_segura(sucia)
        self.assertNotIn("AAEE6XvW", limpia)
        self.assertNotIn("8489029941", limpia)
        self.assertIn("/bot***/sendMessage", limpia)

    def test_no_estropea_una_url_normal(self):
        from core.http import url_segura
        url = "https://www.alkosto.com/tv-kalley/p/123"
        self.assertEqual(url_segura(url), url)

    def test_detecta_el_cambio_a_supergrupo(self):
        """Un grupo que pasa a supergrupo cambia de id y deja al bot mudo."""
        import io
        import contextlib
        from core import telegram
        error = Exception('HTTP 400 -> {"parameters":{"migrate_to_chat_id":-1004476593255}}')
        salida = io.StringIO()
        with contextlib.redirect_stdout(salida):
            telegram._avisar_migracion(error)
        self.assertIn("-1004476593255", salida.getvalue())
        self.assertIn("SUPERGRUPO", salida.getvalue())

    def test_calla_si_el_error_es_otro(self):
        import io
        import contextlib
        from core import telegram
        salida = io.StringIO()
        with contextlib.redirect_stdout(salida):
            telegram._avisar_migracion(Exception("HTTP 500 boom"))
        self.assertEqual(salida.getvalue(), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
