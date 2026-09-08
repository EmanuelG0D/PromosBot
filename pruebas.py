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
    """Un producto, una alerta: aunque cada tienda escriba el titulo distinto."""

    def par(self, **kw):
        from core.scoring import Verdict
        return (oferta(**kw), Verdict(True))

    def test_colapsa_colores_y_tiendas_hermanas(self):
        from radar import _colapsar_variantes
        pares = [
            self.par(key="a", store="Alkosto", price=349900.0,
                     title="Audifonos SONY WH-CH720N Negro"),
            self.par(key="b", store="Alkosto", price=349900.0,
                     title="Audifonos SONY WH-CH720N Azul"),
            self.par(key="c", store="K-tronix", price=349900.0,
                     title="Audifonos SONY WH-CH720N Negro"),
        ]
        unicas, hermanas = _colapsar_variantes(pares)
        self.assertEqual(len(unicas), 1)
        self.assertEqual(len(hermanas[unicas[0][0].key]), 2)
        self.assertIn("Tambien en K-tronix", unicas[0][0].notes)

    def test_junta_el_mismo_producto_con_titulos_distintos(self):
        """Caso real: cada tienda nombra el mismo televisor a su manera."""
        from radar import _colapsar_variantes
        pares = [
            self.par(key="alk", store="Alkosto", price=1249900.0,
                     title='TV KALLEY 50" Pulgadas 126 cm 50G315 4K-UHD MAX LED Smart TV Google'),
            self.par(key="exi", store="Exito", price=1249900.0,
                     title="Televisor Kalley 50G315a 50 Pulgadas 4Kuhd Max Smart Tv"),
        ]
        unicas, _hermanas = _colapsar_variantes(pares)
        self.assertEqual(len(unicas), 1, "el mismo televisor no debe salir dos veces")

    def test_no_junta_productos_distintos_del_mismo_precio(self):
        from radar import _colapsar_variantes
        pares = [
            self.par(key="1", store="Exito", price=99900.0, title="Licuadora IMUSA Powermix"),
            self.par(key="2", store="Exito", price=99900.0, title="Cafetera OSTER 5 tazas"),
        ]
        unicas, _h = _colapsar_variantes(pares)
        self.assertEqual(len(unicas), 2)


class PruebaMarketplaceRedundante(unittest.TestCase):
    """Un revendedor solo interesa si le gana el precio a la tienda."""

    def test_descarta_al_revendedor_que_cobra_igual(self):
        from radar import _marketplace_solo_si_mejora
        propia = oferta(key="p", store="Alkosto", price=1249900.0,
                        title="TV KALLEY 50 Pulgadas 50G315 4K-UHD Smart")
        externo = oferta(key="m", store="Exito", price=1249900.0, marketplace=True,
                         title="Televisor Kalley 50G315a 50 Pulgadas Smart Tv")
        quedan = _marketplace_solo_si_mejora([propia, externo])
        self.assertEqual([d.key for d in quedan], ["p"])

    def test_conserva_al_revendedor_mas_barato(self):
        from radar import _marketplace_solo_si_mejora
        propia = oferta(key="p", store="Alkosto", price=1249900.0,
                        title="TV KALLEY 50 Pulgadas 50G315 4K-UHD Smart")
        externo = oferta(key="m", store="Exito", price=999900.0, marketplace=True,
                         title="Televisor Kalley 50G315a 50 Pulgadas Smart Tv")
        quedan = _marketplace_solo_si_mejora([propia, externo])
        self.assertEqual(sorted(d.key for d in quedan), ["m", "p"])

    def test_no_toca_al_revendedor_de_otro_producto(self):
        from radar import _marketplace_solo_si_mejora
        propia = oferta(key="p", store="Alkosto", price=99900.0, title="Cafetera KALLEY 8 tazas")
        externo = oferta(key="m", store="Exito", price=99900.0, marketplace=True,
                         title="Licuadora IMUSA Powermix 5 velocidades")
        self.assertEqual(len(_marketplace_solo_si_mejora([propia, externo])), 2)


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


class PruebaAlcanceSlickdeals(unittest.TestCase):
    """Slickdeals quedo acotado a Amazon, Adidas, Puma, Nike y ropa.

    Antes eran 22 busquedas de tecnologia y consolas, y la politica de
    "consolas si, videojuegos no" vivia en esas consultas. Al acotarlo, esa
    defensa desaparecio: ahora lo que decide es "incluir", y las consolas
    quedaron sencillamente fuera del alcance.
    """

    @classmethod
    def setUpClass(cls):
        cfg = json.loads((BASE / "watchlist.json").read_text(encoding="utf-8"))
        cls.cfg = cfg["slickdeals"]

    def pasa(self, titulo):
        if filtros.descartado(titulo, self.cfg["excluir"]):
            return False
        return filtros.pertinente(titulo, self.cfg["incluir"])

    def test_entra_lo_que_se_pidio(self):
        for titulo in ("adidas men Supernova Ease Shoes $40 + Free Shipping",
                       "Adidas Women's Adizero SL2 Running Shoes $38",
                       "PUMA Men's Softride Sneakers $29.99",
                       "Nike Men's Revolution 7 Running Shoes $37.97",
                       '$697.99 | 75" Hisense E7 Series 4K TV at Amazon'):
            self.assertTrue(self.pasa(titulo), f"deberia pasar: {titulo}")

    def test_las_mochilas_ya_no_se_vetan(self):
        """Se quitaron backpack, duffel y wallet del veto: ahora interesan."""
        self.assertTrue(self.pasa('17" PUMA Pitch Ball Backpack $16.04'))

    def test_lo_que_quedo_fuera_del_alcance(self):
        """Tecnologia y consolas ya no se buscan aqui; las traen las tiendas
        colombianas, que si publican precio de lista y se pueden verificar."""
        for titulo in ("Nintendo Switch 2 System Black at Woot! $449.99",
                       "Xbox Elite Wireless Controller Series 2 $119",
                       '27" LG Ultragear 1440p 300Hz Monitor $210'):
            self.assertFalse(self.pasa(titulo), f"no deberia entrar: {titulo}")

    def test_el_veto_de_siempre_sigue_en_pie(self):
        for titulo in ("Fragrance Testers and Gift Sets at Woot",
                       "6-Pk PUMA Low-Cut Logo Runner Socks $5.26",
                       "Elden Ring Digital Code (Xbox) $29.99",
                       "Borderlands 4 Super Deluxe Edition $49.99"):
            self.assertFalse(self.pasa(titulo), f"deberia vetarse: {titulo}")

    def test_un_juego_de_amazon_todavia_se_cuela(self):
        """Limitacion conocida, no un descuido.

        "amazon" en incluir deja pasar cualquier cosa de Amazon, juegos
        incluidos, cuando el titulo no usa el vocabulario vetado. Se acepta
        porque es lo que trae tambien el televisor de 75 pulgadas, y porque
        sin precio de lista un juego de $14 nunca gana un cupo de alerta
        frente a una rebaja verificada. Si molesta, es una linea en "excluir".
        """
        self.assertTrue(self.pasa("Biomutant (Nintendo Switch) at Amazon $13.99"))


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
            # "co" y "*" son comodines: /colombia y /todo agrupan fuentes.
            self.assertIn(fuente, set(FUENTES) | {"*", "co"},
                          f"/{comando} apunta a una fuente inexistente")
            self.assertTrue(titulo)

    def test_las_tiendas_existen_en_su_fuente(self):
        from core.comandos import CATALOGO
        from sources import algolia_co, falabella, vtex
        disponibles = {
            "algolia_co": set(algolia_co.TIENDAS),
            "vtex": set(vtex.TIENDAS),
            "droguerias": set(vtex.TIENDAS),      # la drogueria tambien es VTEX
            "falabella": set(falabella.TIENDAS),
        }
        for comando, (fuente, tiendas, _t) in CATALOGO.items():
            if not tiendas:
                continue
            self.assertIn(fuente, disponibles, f"/{comando}: fuente {fuente} sin modulo")
            for tienda in tiendas:
                self.assertIn(tienda, disponibles[fuente], f"/{comando}: {tienda} no existe")

    def test_las_fuentes_de_comunidad_no_exigen_porcentaje(self):
        """Ni Slickdeals ni PROMOCAJITA publican precio de lista.

        Exigirles descuento verificable dejaba /exterior y /cajita siempre
        vacios: su descuento calculado siempre es cero.
        """
        import radar
        from core.models import Deal

        def una(fuente):
            return Deal(source=fuente, store=fuente, country="US", key=fuente,
                        title="Audifonos de prueba", url="u", price=10.0,
                        currency="USD")

        original = radar._ofertas_de
        try:
            for fuente in radar.SIN_PRECIO_DE_LISTA:
                radar._ofertas_de = lambda _w, f, _t: [una(f)]
                self.assertEqual(len(radar._mejores({}, [fuente], None, set(), 5)),
                                 1, fuente)
            # Una tienda normal si tiene que traer un descuento demostrable.
            radar._ofertas_de = lambda _w, _f, _t: [una("vtex")]
            self.assertEqual(radar._mejores({}, ["vtex"], None, set(), 5), [])
        finally:
            radar._ofertas_de = original

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
        from core import comandos
        original = config.TELEGRAM_CHAT_ID
        config.TELEGRAM_CHAT_ID = "-100"
        try:
            propio = {"text": "/alkosto", "chat": {"id": -100}}
            ajeno = {"text": "/alkosto", "chat": {"id": 777}}
            self.assertIsNotNone(comandos.leer_comando(propio))
            self.assertIsNone(comandos.leer_comando(ajeno))
        finally:
            config.TELEGRAM_CHAT_ID = original

    def test_el_menu_no_ofrece_lo_que_se_retiro(self):
        """objetivos y estado se quitaron del menu por pedido del usuario."""
        from core.comandos import MENU
        visibles = {c for c, _d in MENU}
        self.assertNotIn("objetivos", visibles)
        self.assertNotIn("estado", visibles)
        self.assertIn("colombia", visibles)

    def test_colombia_agrupa_las_tiendas_colombianas(self):
        """/colombia debe mirar todas las fuentes del pais, no solo una.

        Antes esta prueba exigia exactamente 5 tiendas y se rompio al sumar
        Jumbo y compania. El numero va a seguir creciendo: lo que hay que
        proteger es que consulte las tres fuentes, y que la ayuda no prometa
        una cifra que quede desactualizada.
        """
        import inspect
        import radar
        from core.comandos import AYUDA, MENU
        codigo = inspect.getsource(radar.atender_solicitudes)
        self.assertIn('if fuente == "co"', codigo)
        for fuente in ("algolia_co", "vtex", "droguerias"):
            self.assertIn(fuente, codigo)
        texto = AYUDA + " ".join(d for _c, d in MENU)
        for cifra in ("cinco tiendas", "5 tiendas"):
            self.assertNotIn(cifra, texto)

    def test_lee_la_cantidad_pedida_en_el_comando(self):
        """/alkosto 25 debe pedir 25, no el valor por defecto."""
        from core import comandos
        original = config.TELEGRAM_CHAT_ID
        config.TELEGRAM_CHAT_ID = "-100"

        def leer(texto):
            return comandos.leer_comando({"text": texto, "chat": {"id": -100}})

        try:
            self.assertEqual(leer("/alkosto 25")["cantidad"], 25)
            # Sin numero manda el valor por defecto, que resuelve el radar.
            self.assertIsNone(leer("/alkosto")["cantidad"])
            # El @ del bot no estorba, y el tope protege del limite de Telegram.
            self.assertEqual(leer("/alkosto@MiBot 9999")["comando"], "alkosto")
            self.assertEqual(leer("/alkosto 9999")["cantidad"],
                             config.COMANDO_MAX_RESULTADOS)
        finally:
            config.TELEGRAM_CHAT_ID = original

    def test_el_tope_protege_del_limite_de_telegram(self):
        import config
        self.assertLessEqual(config.COMANDO_RESULTADOS, config.COMANDO_MAX_RESULTADOS)
        # A 3.5s por tarjeta, el tope debe caber en la corrida de 10 minutos.
        self.assertLess(config.COMANDO_MAX_RESULTADOS * 3.5, 600)

    def test_una_oferta_sin_precio_no_tumba_el_comando(self):
        """Slickdeals anuncia cosas como "Extra 10% off" sin decir el precio.

        Pedir 15 resultados en vez de 5 hizo que esas colaran en la respuesta,
        y calcular() no acepta None: el comando moria con TypeError y el flujo
        de Actions terminaba en rojo.
        """
        import inspect
        import radar
        codigo = inspect.getsource(radar.atender_solicitudes)
        self.assertIn('deal.country == "US" and deal.price', codigo)
        # El guardia es obligatorio porque calcular() no tolera un precio nulo.
        with self.assertRaises(TypeError):
            calcular(oferta(country="US", price=None).price, 4000.0)

    def test_una_barra_sola_no_tumba_la_lectura(self):
        """Un "/" pelado no es un comando, pero tampoco puede reventar.

        El offset se confirma al final de la lectura: si el mensaje la tumba,
        Telegram lo vuelve a entregar y cada corrida falla igual, para siempre.
        """
        from pathlib import Path as _Path
        from core import comandos
        originales = (comandos.ESTADO, comandos.http.get_json,
                      config.TELEGRAM_BOT_TOKEN, config.TELEGRAM_CHAT_ID)
        with tempfile.TemporaryDirectory() as tmp:
            comandos.ESTADO = _Path(tmp) / "estado.json"
            config.TELEGRAM_BOT_TOKEN = "1:x"
            config.TELEGRAM_CHAT_ID = "-100"
            comandos.http.get_json = lambda url, **kw: {"ok": True, "result": [
                {"update_id": 1,
                 "message": {"text": "/", "chat": {"id": -100}}},
                {"update_id": 2,
                 "message": {"text": "/alkosto 25", "chat": {"id": -100}}},
            ]}
            try:
                hallados = comandos.pendientes()
                self.assertEqual(hallados,
                                 [{"comando": "alkosto", "chat_id": -100,
                                   "cantidad": 25}])
                # La barra sola tambien queda confirmada: no vuelve a llegar.
                self.assertEqual(comandos._leer_estado(), 3)
            finally:
                (comandos.ESTADO, comandos.http.get_json,
                 config.TELEGRAM_BOT_TOKEN, config.TELEGRAM_CHAT_ID) = originales


class PruebaMarcas(unittest.TestCase):
    """Una marca corta se cuela dentro de palabras corrientes."""

    def test_puma_no_es_espuma(self):
        """Caso real: buscar "puma" en Olimpica devolvia colchones
        "esPUMAdos" y adaptadores "ComPUMAx". De 50 resultados, cero eran
        PUMA."""
        from core import filtros
        for falso in ("Colchon Nova Espumados Del Litoral 140x190",
                      "Adaptador Corriente 12v2a Compumax 11054",
                      "Espuma Facial Granulos 120Gr Kaloe Exfoliante"):
            self.assertFalse(filtros.menciona(falso, "puma"), falso)
        for real in ("PUMA Men Softride Sneakers",
                     "Tenis Puma Caven 2.0 Hombre"):
            self.assertTrue(filtros.menciona(real, "puma"), real)

    def test_reconoce_marcas_de_dos_palabras(self):
        from core import filtros
        self.assertTrue(filtros.menciona("Zapatillas New Balance 574", "new balance"))
        self.assertFalse(filtros.menciona("Balance de cocina digital", "new balance"))

    def test_no_confunde_una_marca_con_el_principio_de_otra_palabra(self):
        from core import filtros
        self.assertFalse(filtros.menciona("Camiseta Niketown generica", "nike"))
        self.assertTrue(filtros.menciona("Tenis Nike Zoom Vomero 5", "nike"))

    def test_el_perfume_no_es_ropa(self):
        """Las marcas deportivas venden perfume, y buscar "adidas" lo traia."""
        from core import filtros
        self.assertTrue(filtros.descartado(
            "Perfume Adidas Hombre Ice Dive Eau De Toilette", filtros.VETADAS_CO))


class PruebaRitmos(unittest.TestCase):
    """Dos ritmos y un horario: ni todo cambia al mismo paso, ni de
    madrugada hay nada que buscar."""

    def test_de_madrugada_no_sale_a_buscar(self):
        """Un comando a las 3 a.m. despierta el servicio; sin esta guarda
        arrancaria una ronda completa contra las 12 tiendas."""
        import datetime as _dt
        import server
        def a_las(hora):
            return server.en_horario(_dt.datetime(2026, 9, 8, hora,
                                                  tzinfo=server.ZONA_CO))
        for dormida in (0, 3, 5):
            self.assertFalse(a_las(dormida), f"{dormida}:00 deberia estar fuera")
        for despierta in (6, 12, 23):
            self.assertTrue(a_las(despierta), f"{despierta}:00 deberia entrar")

    def test_los_dos_ritmos_cubren_todo_sin_repetir(self):
        """Una fuente en los dos ritmos se consultaria de mas; una fuente en
        ninguno no se consultaria nunca, y nadie se enteraria."""
        import radar
        import server
        rapido, lento = set(server.RONDA_COMUNIDAD), set(server.RONDA_CATALOGOS)
        self.assertEqual(rapido | lento, set(radar.FUENTES))
        self.assertEqual(rapido & lento, set())

    def test_lo_que_cambia_rapido_va_en_el_ritmo_rapido(self):
        """Las ofertas de comunidad duran horas; un catalogo de tienda se
        mueve por dia. Cambiarlas de ritmo seria gastar peticiones."""
        import server
        self.assertIn("promocajita", server.RONDA_COMUNIDAD)
        self.assertIn("slickdeals", server.RONDA_COMUNIDAD)
        self.assertIn("vtex", server.RONDA_CATALOGOS)
        self.assertLess(server.INTERVALO_COMUNIDAD_MIN,
                        server.INTERVALO_CATALOGOS_MIN)

    def test_la_ronda_programada_espera_su_turno(self):
        """Descartar la ronda de catalogos por chocar con una de comunidad
        costaria una hora entera."""
        import inspect
        import server
        codigo = inspect.getsource(server.programador)
        self.assertIn("espera_s=", codigo)


class PruebaPromocajita(unittest.TestCase):
    """El canal de Telegram de la comunidad colombiana de ofertas."""

    BLOQUE = (
        '<div class="tgme_widget_message_photo_wrap" '
        'style="background-image:url(https://cdn1.telesco.pe/file/abc)"></div>'
        '<a class="tgme_widget_message_date" href="https://t.me/cajitatech/84260">'
        '<time datetime="2026-09-08T15:42:58+00:00"></time></a>'
        '<div class="tgme_widget_message_text js-message_text">'
        '#Tablet 💰 $679915 COP - Redmi Pad 2 (4+128) + Cover '
        '🛍 Aplicar XIAOMILOVERS para obtener el descuento '
        'Oferta ➡ <a href="https://pccajita.link/k44dla">link</a></div>'
    )

    def test_el_punto_en_pesos_no_son_centavos(self):
        """En COP el punto separa miles; en USD son los centavos. Tratarlos
        igual multiplicaba por cien el precio en dolares."""
        from sources.promocajita import _monto
        self.assertEqual(_monto("679.915", "COP"), 679915.0)
        self.assertEqual(_monto("679915", "COP"), 679915.0)
        self.assertEqual(_monto("1,299.99", "USD"), 1299.99)
        self.assertEqual(_monto("3.97", "USD"), 3.97)

    def test_lee_una_publicacion_completa(self):
        from sources import promocajita
        deal = promocajita._una_publicacion("cajitatech/84260", self.BLOQUE)
        self.assertIsNotNone(deal)
        self.assertEqual(deal.price, 679915.0)
        self.assertEqual(deal.currency, "COP")
        self.assertEqual(deal.country, "CO")
        self.assertIn("Redmi Pad 2", deal.title)
        # El titulo corta antes de la mecanica de la oferta.
        self.assertNotIn("Aplicar", deal.title)
        self.assertEqual(deal.coupons, ["XIAOMILOVERS"])
        self.assertEqual(deal.url, "https://pccajita.link/k44dla")
        self.assertEqual(deal.key, "promocajita:cajitatech/84260")
        # Nadie publica el precio anterior: fingirlo seria inventar el descuento.
        self.assertIsNone(deal.list_price)
        self.assertEqual(deal.discount_verificable, 0.0)

    def test_incluir_es_lo_que_evita_que_ahogue_el_canal(self):
        """Publican decenas de cosas al dia; sin este filtro se comerian los
        cupos de alerta con floreros."""
        from sources import promocajita
        pasa = promocajita._una_publicacion("c/1", self.BLOQUE, incluir=["tablet"])
        no_pasa = promocajita._una_publicacion("c/1", self.BLOQUE, incluir=["nevera"])
        self.assertIsNotNone(pasa)
        self.assertIsNone(no_pasa)

    def test_sin_precio_no_es_oferta(self):
        from sources import promocajita
        sin = self.BLOQUE.replace("$679915 COP", "precio en el enlace")
        self.assertIsNone(promocajita._una_publicacion("c/1", sin))


class PruebaFalabella(unittest.TestCase):
    """Falabella y Homecenter: mismo JSON incrustado, formatos distintos."""

    def test_el_precio_con_tarjeta_no_es_el_precio(self):
        """Usar el precio CMR inflaria el descuento de una oferta que exige
        tener esa tarjeta. Va como nota, no como precio."""
        from sources import falabella
        precios = [
            {"type": "internetPrice", "crossed": False, "price": ["2.699.900"]},
            {"type": "normalPrice", "crossed": True, "price": ["4.999.900"]},
            {"type": "cmrPrice", "crossed": False, "price": ["2.599.900"]},
        ]
        precio, lista, tarjeta = falabella._precios(precios)
        self.assertEqual(precio, 2699900.0)
        self.assertEqual(lista, 4999900.0)
        self.assertEqual(tarjeta, 2599900.0)

    def test_lee_los_dos_formatos_de_precio(self):
        """Homecenter regala el numero limpio; Falabella solo el texto, y a
        veces dentro de una lista."""
        from sources import falabella
        self.assertEqual(falabella._numero({"priceWithoutFormatting": 2199900}), 2199900.0)
        self.assertEqual(falabella._numero({"price": "3.999.900"}), 3999900.0)
        self.assertEqual(falabella._numero({"price": ["3.199.900"]}), 3199900.0)
        self.assertIsNone(falabella._numero({"price": None}))

    def test_homecenter_marca_la_lista_con_NORMAL(self):
        """Homecenter no usa 'crossed': rotula el precio de lista como NORMAL."""
        from sources import falabella
        precios = [
            {"type": "INTERNET", "priceWithoutFormatting": 2199900},
            {"label": "Normal", "type": "NORMAL", "priceWithoutFormatting": 3999900},
        ]
        precio, lista, _tarjeta = falabella._precios(precios)
        self.assertEqual((precio, lista), (2199900.0, 3999900.0))

    def test_la_foto_se_pide_en_jpeg(self):
        """El CDN de Falabella sirve WebP y sendPhoto no lo acepta: lo trata
        como sticker. La oferta llegaba, pero sin foto."""
        from sources import falabella
        base = "https://media.falabella.com.co/falabellaCO/73677567_1/public"
        self.assertEqual(falabella._foto([base]), base + "?format=jpg")
        # Si la URL ya trae parametros, se encadena bien.
        self.assertEqual(falabella._foto([base + "?w=800"]),
                         base + "?w=800&format=jpg")
        self.assertIsNone(falabella._foto([]))
        self.assertIsNone(falabella._foto(None))

    def test_encuentra_los_productos_donde_sea(self):
        """Falabella los deja cerca de la raiz y Homecenter tres niveles
        adentro; la ruta no es un contrato, la forma del dato si."""
        from sources import falabella
        hondo = {"props": {"pageProps": {"searchProps": {"searchData": {
            "results": [{"displayName": "Taladro", "prices": []}]}}}}}
        somero = {"pageProps": {"results": [{"displayName": "Televisor", "prices": []}]}}
        self.assertEqual(falabella._productos(hondo)[0]["displayName"], "Taladro")
        self.assertEqual(falabella._productos(somero)[0]["displayName"], "Televisor")
        # Una lista de "results" que no son productos no cuenta.
        self.assertIsNone(falabella._productos({"results": [{"otra": "cosa"}]}))


class PruebaRuido(unittest.TestCase):
    """Lo que separa el producto de lo que lo acompaña."""

    def test_el_accesorio_tiene_que_abrir_el_titulo(self):
        """Buscar la palabra en cualquier parte descartaba el producto.

        "Aspiradora T-FAL X-FORCE, bateria 45 min, 4 accesorios" es una
        aspiradora. "Base KALLEY para televisores 23 a 55" es un soporte. La
        diferencia esta en si la palabra abre el titulo o solo se menciona.
        """
        from core import filtros
        producto = "Aspiradora Inalambrica T-FAL X-FORCE FLEX, 250W, 4 accesorios"
        accesorio = 'Base KALLEY Brazo Flexible para televisores 23" a 55"'
        mueble = 'Rack TV 45" Soho Bellota - RTA Design'
        self.assertFalse(filtros.es_accesorio(producto))
        self.assertTrue(filtros.es_accesorio(accesorio))
        # El mueble donde va el televisor tampoco es un televisor.
        self.assertTrue(filtros.es_accesorio(mueble))

    def test_una_lista_vacia_no_veta_nada(self):
        """La drogueria pide ver todo: el veto general le tumbaria el
        maquillaje ("Base liquida Samy") y el protector solar."""
        import radar
        from core.models import Deal
        def oferta_de(titulo):
            return Deal(source="vtex", store="La Rebaja", country="CO",
                        key="k", title=titulo, url="u", price=1.0, currency="COP")
        ofertas = [oferta_de("BASE LIQUIDA SAMY GO BRIGHT"),
                   oferta_de("PROTECTOR SOLAR FACIAL SPF 50")]
        self.assertEqual(len(radar._sin_ruido(ofertas, {"excluir": []})), 2)
        # Sin configuracion propia si aplica el veto general.
        self.assertEqual(len(radar._sin_ruido(ofertas, {})), 0)

    def test_las_tiendas_de_la_watchlist_existen(self):
        """Un nombre mal escrito se descubriria en produccion, no aqui."""
        import config
        from sources import algolia_co, falabella, vtex
        w = config.load_watchlist()
        conocidas = {"vtex": set(vtex.TIENDAS), "droguerias": set(vtex.TIENDAS),
                     "algolia_co": set(algolia_co.TIENDAS),
                     "falabella": set(falabella.TIENDAS)}
        for fuente, validas in conocidas.items():
            for tienda in (w.get(fuente) or {}).get("tiendas") or []:
                self.assertIn(tienda, validas, f"{fuente}: {tienda}")

    def test_la_drogueria_pide_el_catalogo_entero(self):
        """No se le buscan terminos: se le pide lo mas rebajado de la tienda."""
        import config
        from sources import vtex
        cfg = config.load_watchlist().get("droguerias") or {}
        self.assertEqual(cfg.get("queries"), [vtex.CATALOGO])
        self.assertNotIn("ft=", vtex.RUTA_CATALOGO)


class PruebaWebhook(unittest.TestCase):
    """El webhook es la puerta por la que entran los comandos: si acepta
    cualquier cosa, cualquiera pone al bot a trabajar para el."""

    def _servidor(self):
        """Levanta el manejador real en un puerto libre de la maquina."""
        import threading as _th
        from http.server import ThreadingHTTPServer
        import server
        srv = ThreadingHTTPServer(("127.0.0.1", 0), server.Manejador)
        srv.daemon_threads = True
        _th.Thread(target=srv.serve_forever, daemon=True).start()
        # addCleanup corre al reves de como se registra: primero hay que
        # parar el bucle y solo despues cerrar el socket que escucha. Al
        # reves, el servidor atendia con el socket ya cerrado y la prueba
        # fallaba una de cada tres veces.
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return f"http://127.0.0.1:{srv.server_address[1]}", server

    def _post(self, url, cuerpo, secreto=None):
        import urllib.error
        import urllib.request
        cabeceras = {"Content-Type": "application/json"}
        if secreto is not None:
            cabeceras["X-Telegram-Bot-Api-Secret-Token"] = secreto
        peticion = urllib.request.Request(
            url, data=json.dumps(cuerpo).encode(), headers=cabeceras, method="POST")
        try:
            with urllib.request.urlopen(peticion, timeout=5) as r:
                return r.status
        except urllib.error.HTTPError as exc:
            return exc.code

    def test_sin_el_secreto_no_entra_nada(self):
        from core import telegram
        base, servidor = self._servidor()
        mensaje = {"update_id": 1, "message": {"text": "/alkosto", "chat": {"id": 1}}}

        self.assertEqual(self._post(base + servidor.RUTA_WEBHOOK, mensaje), 403)
        self.assertEqual(
            self._post(base + servidor.RUTA_WEBHOOK, mensaje, "no-es-el-secreto"), 403)
        # Con el secreto correcto se acepta al instante, aunque el mensaje no
        # sea del chat configurado (eso lo filtra leer_comando despues).
        self.assertEqual(
            self._post(base + servidor.RUTA_WEBHOOK, mensaje,
                       telegram.secreto_webhook()), 200)

    def test_el_secreto_no_revela_el_token(self):
        """Se deriva del token con un hash: estable entre reinicios, y no hay
        forma de devolverlo al token."""
        from core import telegram
        original = config.TELEGRAM_BOT_TOKEN, config.TELEGRAM_WEBHOOK_SECRET
        config.TELEGRAM_BOT_TOKEN = "8489029941:AAEE6XvWabcdefghijklmnop"
        config.TELEGRAM_WEBHOOK_SECRET = ""
        try:
            secreto = telegram.secreto_webhook()
            self.assertNotIn("AAEE6XvW", secreto)
            self.assertNotIn("8489029941", secreto)
            self.assertEqual(secreto, telegram.secreto_webhook())   # estable
        finally:
            config.TELEGRAM_BOT_TOKEN, config.TELEGRAM_WEBHOOK_SECRET = original

    def test_un_comando_repetido_se_responde_una_sola_vez(self):
        """Telegram reintenta una entrega si duda de que llegara."""
        import server
        original = list(server._atendidos)
        try:
            server._atendidos.clear()
            self.assertFalse(server._ya_atendido(7))
            self.assertTrue(server._ya_atendido(7))
            # La memoria no crece sin control.
            for i in range(server._ATENDIDOS_MAX + 50):
                server._ya_atendido(1000 + i)
            self.assertLessEqual(len(server._atendidos), server._ATENDIDOS_MAX)
        finally:
            server._atendidos[:] = original


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


class PruebaMarketplace(unittest.TestCase):
    """Cuando creerle el precio de lista a un vendedor externo.

    El mismo televisor Kalley aparece con identico precio de lista en la tienda
    propia de Alkosto y en el marketplace del Exito, asi que ese numero es real
    y descartarlo costaba ofertas legitimas. Pero los accesorios baratos al
    70-80% si son precio inflado.
    """
    TIENDA = {"nombre": "Exito", "api": "x", "web": "https://www.exito.com"}

    def producto(self, precio, lista, seller="2"):
        return {"linkText": "x", "productId": "1", "productName": "P",
                "items": [{"images": [], "sellers": [{"sellerId": seller, "sellerName": "s",
                    "commertialOffer": {"Price": precio, "ListPrice": lista,
                                        "AvailableQuantity": 5, "IsAvailable": True}}]}]}

    def confiable(self, precio, lista, seller="2"):
        from sources.vtex import _consultar
        import sources.vtex as v
        original = v.http.get_json
        v.http.get_json = lambda *a, **k: [self.producto(precio, lista, seller)]
        try:
            return _consultar("exito", self.TIENDA, "q", 10)[0].list_price_trusted
        finally:
            v.http.get_json = original

    def test_le_cree_al_televisor_caro_con_rebaja_creible(self):
        self.assertTrue(self.confiable(1_249_900, 3_099_900))   # -59.7%, caso real

    def test_no_le_cree_al_accesorio_barato(self):
        self.assertFalse(self.confiable(38_718, 110_000))       # soporte de pared

    def test_no_le_cree_al_descuento_absurdo(self):
        self.assertFalse(self.confiable(1_679_990, 36_799_990))  # el portatil falso

    def test_a_la_tienda_propia_si_le_cree(self):
        self.assertTrue(self.confiable(64_900, 184_900, seller="1"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
