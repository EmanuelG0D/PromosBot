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
        for comando in ("alkosto", "ktronix", "exito", "carulla", "olimpica", "exterior", "mercadolibre"):
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

    def test_teclados_telegram_bien_formados(self):
        """Los teclados interactivos deben tener las opciones correctas y ser minimizables."""
        from core import telegram
        from sources import vtex
        tiendas = telegram.teclado_tiendas()
        self.assertTrue(tiendas.get("resize_keyboard"))
        self.assertFalse(tiendas.get("is_persistent", False))
        botones_tiendas = [b["text"] for row in tiendas["keyboard"] for b in row]
        self.assertIn("🟡 Éxito", botones_tiendas)
        self.assertIn("🔴 Alkosto", botones_tiendas)
        self.assertIn("🟢 Carulla", botones_tiendas)
        self.assertIn("🟠 Homecenter", botones_tiendas)
        self.assertIn("📦 Promocajita", botones_tiendas)
        self.assertIn("🇨🇴 Comparar Tiendas", botones_tiendas)
        self.assertNotIn("🎯 Mis Objetivos", botones_tiendas)
        self.assertNotIn("pepeganga", vtex.TIENDAS)

        categorias = telegram.teclado_categorias("Éxito")
        self.assertTrue(categorias.get("resize_keyboard"))
        self.assertFalse(categorias.get("is_persistent", False))
        botones_cat = [b["text"] for row in categorias["keyboard"] for b in row]
        self.assertIn("🌟 TODO", botones_cat)
        self.assertIn("📺 Smart TV", botones_cat)
        self.assertIn("⬅️ Volver a Tiendas", botones_cat)

    def test_botones_menu_interactivo_se_interpretan_correctamente(self):
        """El parser debe entender cuando el usuario toca un boton en vez de escribir un comando."""
        from core import comandos
        original = config.TELEGRAM_CHAT_ID
        config.TELEGRAM_CHAT_ID = "-100"
        try:
            def leer(t):
                return comandos.leer_comando({"text": t, "chat": {"id": -100}})

            # Tienda
            res = leer("🟡 Éxito")
            self.assertEqual(res["tipo"], "elegir_tienda")
            self.assertEqual(res["tienda"], "exito")

            # Promocajita (ahora con menu de categorias)
            res_cajita = leer("📦 Promocajita")
            self.assertEqual(res_cajita["tipo"], "elegir_tienda")
            self.assertEqual(res_cajita["tienda"], "cajita")

            # Categoria
            res_cat = leer("📺 Smart TV")
            self.assertEqual(res_cat["tipo"], "categoria")
            self.assertIn("televisor", res_cat["consultas"])

            # Categoria sin emoji
            res_cat2 = leer("smart tv")
            self.assertEqual(res_cat2["tipo"], "categoria")

            # Todo
            res_todo = leer("🌟 TODO")
            self.assertEqual(res_todo["tipo"], "todo_tienda")

            # Volver
            res_volver = leer("⬅️ Volver a Tiendas")
            self.assertEqual(res_volver["tipo"], "menu")

            # Objetivos
            res_obj = leer("🎯 Mis Objetivos")
            self.assertEqual(res_obj["comando"], "objetivos")
        finally:
            config.TELEGRAM_CHAT_ID = original

    def test_limite_precio_maximo(self):
        """No deben pasar ofertas de mas de 2 millones de pesos."""
        import radar
        barato = oferta(price=1_500_000.0, currency="COP")
        caro = oferta(price=2_500_000.0, currency="COP")
        caro_usd = oferta(price=700.0, currency="USD")  # 700 * 4000 = 2.800.000 COP
        barato_usd = oferta(price=300.0, currency="USD") # 300 * 4000 = 1.200.000 COP

        self.assertTrue(radar._precio_admisible(barato, trm=4000.0))
        self.assertFalse(radar._precio_admisible(caro, trm=4000.0))
        self.assertTrue(radar._precio_admisible(barato_usd, trm=4000.0))
        self.assertFalse(radar._precio_admisible(caro_usd, trm=4000.0))

    def test_topes_categoria_precio_objetivos(self):
        """Valida que los objetivos reconozcan las metas fijadas por el usuario."""
        import config
        from core import objetivos
        metas = config.load_watchlist().get("objetivos", [])
        # Ropa: < 50.000
        self.assertIsNotNone(objetivos.alcanzado(oferta(title="KOAJ Camiseta azul", price=18130.0), metas))
        self.assertIsNotNone(objetivos.alcanzado(oferta(title="Camiseta Polo Lacoste", price=45000.0), metas))
        self.assertIsNone(objetivos.alcanzado(oferta(title="Camiseta Polo Lacoste", price=65000.0), metas))
        # Zapatos: < 150.000
        self.assertIsNotNone(objetivos.alcanzado(oferta(title="Tenis Running Puma", price=140000.0), metas))
        self.assertIsNone(objetivos.alcanzado(oferta(title="Tenis Nike Air Jordan", price=250000.0), metas))
        # TV: < 1.500.000
        self.assertIsNotNone(objetivos.alcanzado(oferta(title="Smart TV Xiaomi 43", price=1100000.0), metas))
        self.assertIsNone(objetivos.alcanzado(oferta(title="Televisor OLED LG 55", price=1800000.0), metas))
        # Monitores: < 800.000
        self.assertIsNotNone(objetivos.alcanzado(oferta(title="Monitor Gamer 24 LG", price=650000.0), metas))
        self.assertIsNone(objetivos.alcanzado(oferta(title="Monitor Samsung 34 Curvo", price=1200000.0), metas))
        # Lavadoras: < 1.500.000
        self.assertIsNotNone(objetivos.alcanzado(oferta(title="Lavadora Whirlpool 16kg", price=1350000.0), metas))
        self.assertIsNone(objetivos.alcanzado(oferta(title="Lavadora Secadora LG", price=2200000.0), metas))
        # Neveras: < 1.800.000
        self.assertIsNotNone(objetivos.alcanzado(oferta(title="Nevera Haceb 250L", price=1450000.0), metas))
        self.assertIsNone(objetivos.alcanzado(oferta(title="Nevera Samsung French Door", price=3500000.0), metas))
        # Electrodomésticos pequeños: < 250.000
        self.assertIsNotNone(objetivos.alcanzado(oferta(title="Freidora de Aire Imusa", price=199900.0), metas))
        self.assertIsNone(objetivos.alcanzado(oferta(title="Freidora Ninja Dual", price=550000.0), metas))

    def test_persistencia_tienda_activa(self):
        """La tienda seleccionada se guarda en el estado para recordar contexto entre mensajes."""
        import tempfile
        from pathlib import Path as _Path
        from core import comandos
        with tempfile.TemporaryDirectory() as tmp:
            original = comandos.ESTADO
            comandos.ESTADO = _Path(tmp) / "estado.json"
            try:
                self.assertEqual(comandos.tienda_activa(), "colombia")
                comandos.fijar_tienda_activa("alkosto")
                self.assertEqual(comandos.tienda_activa(), "alkosto")
            finally:
                comandos.ESTADO = original

    def test_atender_solicitudes_flujo_interactivo(self):
        """El flujo de seleccion de tienda y categoria despacha mensajes con teclado."""
        import tempfile
        from pathlib import Path as _Path
        import radar
        from core import comandos, telegram

        mensajes_enviados = []
        original_send = telegram.send
        original_estado = comandos.ESTADO
        try:
            telegram.send = lambda texto, preview=False, reply_markup=None, **k: mensajes_enviados.append((texto, reply_markup)) or True
            with tempfile.TemporaryDirectory() as tmp:
                comandos.ESTADO = _Path(tmp) / "estado.json"

                # 1. Menu
                radar.atender_solicitudes([{"comando": "menu", "tipo": "menu", "chat_id": -100}])
                self.assertTrue(any("Menú Principal" in m[0] for m in mensajes_enviados))
                self.assertIsNotNone(mensajes_enviados[-1][1])

                # 2. Elegir tienda
                mensajes_enviados.clear()
                radar.atender_solicitudes([{
                    "comando": "elegir_tienda",
                    "tienda": "exito",
                    "tienda_nombre": "Éxito",
                    "tipo": "elegir_tienda",
                    "chat_id": -100
                }])
                self.assertEqual(comandos.tienda_activa(), "exito")
                self.assertTrue(any("Éxito seleccionado" in m[0] for m in mensajes_enviados))
                self.assertIsNotNone(mensajes_enviados[-1][1])
        finally:
            telegram.send = original_send
            comandos.ESTADO = original_estado


class PruebaTodasLasFuentesLlegan(unittest.TestCase):
    """Una fuente configurada tiene que llegar a su modulo.

    PROMOCAJITA se configura con "canales" y no con "queries", y un guardia
    generico la descartaba antes de llegar a su rama: /cajita devolvia vacio
    siempre, y /colombia y /todo la saltaban en silencio. Nadie se enteraba
    porque no hay error, solo ausencia.
    """

    def test_cada_fuente_configurada_llama_a_su_modulo(self):
        import config
        import radar
        w = config.load_watchlist()
        modulos = {"algolia_co": "algolia_co", "vtex": "vtex",
                   "falabella": "falabella", "droguerias": "vtex",
                   "slickdeals": "slickdeals", "promocajita": "promocajita"}
        for fuente, modulo in modulos.items():
            cfg = w.get(fuente) or {}
            if not (cfg.get("queries") or cfg.get("canales")):
                continue                       # sin configurar, no aplica
            mod = getattr(radar, modulo)
            original, llamadas = mod.fetch, []
            try:
                mod.fetch = lambda *a, _l=llamadas, **k: _l.append(1) or []
                radar._ofertas_de(w, fuente, None)
            finally:
                mod.fetch = original
            self.assertEqual(len(llamadas), 1,
                             f"{fuente} no llego a {modulo}.fetch()")


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


class PruebaRespuestaInmediataYCancelacion(unittest.TestCase):
    def test_notificar_inicio_busqueda_marca_notificado(self):
        import server
        from core import telegram
        enviados = []
        orig_send = telegram.send
        orig_esc = telegram.accion_escribiendo
        telegram.send = lambda txt, **k: enviados.append(txt) or True
        telegram.accion_escribiendo = lambda *a, **k: None
        try:
            reqs = [
                {"tipo": "categoria", "categoria_nombre": "👟 Zapatos y Tenis"},
                {"tipo": "todo_tienda"},
                {"comando": "alkosto", "tipo": "slash"},
            ]
            server._notificar_inicio_busqueda(reqs)
            for r in reqs:
                self.assertTrue(r.get("notificado"))
            self.assertEqual(len(enviados), 3)
            self.assertIn("Zapatos y Tenis", enviados[0])
            self.assertIn("catálogo", enviados[1])
            self.assertIn("Alkosto", enviados[2])
        finally:
            telegram.send = orig_send
            telegram.accion_escribiendo = orig_esc

    def test_nueva_busqueda_incrementa_token_y_cancela(self):
        import radar
        t1 = radar.nueva_busqueda()
        t2 = radar.nueva_busqueda()
        self.assertGreater(t2, t1)


class PruebaMercadoLibre(unittest.TestCase):
    def test_teclado_tiendas_incluye_mercadolibre(self):
        from core import telegram
        teclado = telegram.teclado_tiendas()
        botones = [btn["text"] for fila in teclado.get("keyboard", []) for btn in fila]
        self.assertIn("💛 Mercado Libre", botones)

    def test_boton_mercadolibre_abre_tienda(self):
        import config
        from core import comandos
        req = comandos.leer_comando({"text": "💛 Mercado Libre", "chat": {"id": config.TELEGRAM_CHAT_ID}})
        self.assertIsNotNone(req)
        self.assertEqual(req.get("tipo"), "elegir_tienda")
        self.assertEqual(req.get("tienda"), "mercadolibre")

    def test_mapeo_categorias_mercadolibre(self):
        from sources import mercadolibre
        self.assertEqual(mercadolibre._categoria_para_consultas(["smart tv"]), "MCO1000")
        self.assertEqual(mercadolibre._categoria_para_consultas(["portatil"]), "MCO1648")
        self.assertEqual(mercadolibre._categoria_para_consultas(["celular"]), "MCO1051")
        self.assertEqual(mercadolibre._categoria_para_consultas(["tenis"]), "MCO1276")
        self.assertEqual(mercadolibre._categoria_para_consultas(["nevera"]), "MCO5726")

    def test_parsea_estado_con_ofertas_y_cupones(self):
        from sources import mercadolibre
        from core import http
        import json

        mock_data = {
            "appProps": {
                "pageProps": {
                    "data": {
                        "items": [
                            {
                                "card": {
                                    "metadata": {
                                        "id": "MCO9999",
                                        "url": "https://articulo.mercadolibre.com.co/MCO-9999-producto",
                                    },
                                    "pictures": {"pictures": [{"id": "123456-MCO"}]},
                                    "components": [
                                        {"type": "title", "title": {"text": "Smart TV Samsung 55 4K UHD"}},
                                        {
                                            "type": "price",
                                            "price": {
                                                "current_price": {"value": 1500000.0},
                                                "price_labels": [
                                                    {
                                                        "values": [
                                                            {"key": "previous_price", "price": {"value": 3000000.0}}
                                                        ]
                                                    }
                                                ],
                                            },
                                        },
                                        {
                                            "type": "promotions",
                                            "promotions": [{"text": "{icon_cockade} Cupón 10% OFF"}],
                                        },
                                        {
                                            "type": "seller",
                                            "seller": {"text": "Tienda Oficial Samsung"},
                                        },
                                    ],
                                }
                            }
                        ]
                    }
                }
            }
        }
        mock_html = f"<html><body><script>_n.ctx.r = {json.dumps(mock_data)};</script></body></html>"

        orig_get_text = http.get_text
        try:
            http.get_text = lambda url, **k: mock_html
            deals = mercadolibre.fetch(None, por_consulta=10)
            self.assertEqual(len(deals), 1)
            d = deals[0]
            self.assertEqual(d.source, "mercadolibre")
            self.assertEqual(d.store, "Mercado Libre")
            self.assertEqual(d.country, "CO")
            self.assertEqual(d.currency, "COP")
            self.assertEqual(d.price, 1500000.0)
            self.assertEqual(d.list_price, 3000000.0)
            self.assertEqual(d.discount_verificable, 50.0)
            self.assertIn("https://http2.mlstatic.com/D_NQ_NP_123456-MCO-F.jpg", d.image)
            self.assertTrue(any("Cupón 10% OFF" in n for n in d.notes))
            self.assertFalse(any("{icon_cockade}" in n for n in d.notes))
            self.assertIn("Tienda Oficial Samsung", d.notes)
        finally:
            http.get_text = orig_get_text


class PruebaControlAccesoWhitelist(unittest.TestCase):
    """Pruebas del sistema de control de acceso por whitelist y aprobacion interactiva."""

    def test_es_admin_identifica_al_dueno(self):
        from core import whitelist
        orig_admin = config.TELEGRAM_ADMIN_ID
        try:
            config.TELEGRAM_ADMIN_ID = "5583002220"
            self.assertTrue(whitelist.es_admin("5583002220"))
            self.assertTrue(whitelist.es_admin(5583002220))
            self.assertFalse(whitelist.es_admin("9999999999"))
            self.assertFalse(whitelist.es_admin(None))
        finally:
            config.TELEGRAM_ADMIN_ID = orig_admin

    def test_ciclo_solicitud_aprobacion_y_rechazo(self):
        from core import whitelist
        import tempfile
        from pathlib import Path as _Path

        with tempfile.TemporaryDirectory() as tmp:
            orig_archivo = whitelist.ARCHIVO_LOCAL
            whitelist.ARCHIVO_LOCAL = _Path(tmp) / "whitelist_test.json"
            orig_admin = config.TELEGRAM_ADMIN_ID
            config.TELEGRAM_ADMIN_ID = "5583002220"
            try:
                # 1. El admin siempre es permitido
                self.assertTrue(whitelist.es_permitido("5583002220"))
                # Un extrano no
                self.assertFalse(whitelist.es_permitido("12345"))

                # 2. Registrar solicitud nueva
                es_nueva = whitelist.registrar_solicitud("12345", nombre="Carlos", username="carlos_dev")
                self.assertTrue(es_nueva)
                self.assertFalse(whitelist.es_permitido("12345"))

                # Si repite, es_nueva debe ser False
                self.assertFalse(whitelist.registrar_solicitud("12345", nombre="Carlos"))

                # 3. Aprobar usuario
                info = whitelist.aprobar("12345")
                self.assertEqual(info.get("nombre"), "Carlos")
                self.assertTrue(whitelist.es_permitido("12345"))

                # 4. Rechazar / Revocar usuario
                info_rev = whitelist.rechazar("12345")
                self.assertFalse(whitelist.es_permitido("12345"))
            finally:
                whitelist.ARCHIVO_LOCAL = orig_archivo
                config.TELEGRAM_ADMIN_ID = orig_admin

    def test_leer_comando_control_acceso_privado(self):
        from core import comandos, telegram, whitelist
        import tempfile
        from pathlib import Path as _Path

        with tempfile.TemporaryDirectory() as tmp:
            orig_wl = whitelist.ARCHIVO_LOCAL
            whitelist.ARCHIVO_LOCAL = _Path(tmp) / "whitelist_test.json"
            orig_admin = config.TELEGRAM_ADMIN_ID
            config.TELEGRAM_ADMIN_ID = "5583002220"
            orig_miembro = telegram.es_miembro_del_canal
            try:
                msg_extrano = {
                    "text": "/alkosto",
                    "chat": {"id": 8888, "type": "private"},
                    "from": {"id": 8888, "first_name": "Pedro", "username": "pedro88"},
                }

                # Caso 1: Pedro NO está en el canal oficial -> debe ser rechazado con unirse_canal
                telegram.es_miembro_del_canal = lambda uid, **k: False
                solicitud_canal = comandos.leer_comando(msg_extrano)
                self.assertIsNotNone(solicitud_canal)
                self.assertEqual(solicitud_canal.get("tipo"), "unirse_canal")
                self.assertEqual(solicitud_canal.get("user_id"), 8888)

                # Caso 2: Pedro SÍ se une al canal oficial pero no está en whitelist -> solicitud_acceso
                telegram.es_miembro_del_canal = lambda uid, **k: True
                solicitud = comandos.leer_comando(msg_extrano)
                self.assertIsNotNone(solicitud)
                self.assertEqual(solicitud.get("tipo"), "solicitud_acceso")
                self.assertTrue(solicitud.get("es_nueva"))
                self.assertEqual(solicitud.get("user_id"), 8888)

                # Si escribe de nuevo mientras sigue pendiente
                solicitud2 = comandos.leer_comando(msg_extrano)
                self.assertFalse(solicitud2.get("es_nueva"))

                # El admin aprueba a Pedro
                whitelist.aprobar("8888")

                # Ahora Pedro escribe /alkosto en privado y es atendido normalmente
                solicitud3 = comandos.leer_comando(msg_extrano)
                self.assertIsNotNone(solicitud3)
                self.assertEqual(solicitud3.get("comando"), "alkosto")
                self.assertEqual(solicitud3.get("chat_id"), 8888)
            finally:
                whitelist.ARCHIVO_LOCAL = orig_wl
                config.TELEGRAM_ADMIN_ID = orig_admin
                telegram.es_miembro_del_canal = orig_miembro

    def test_sesion_aislada_por_chat(self):
        from core import comandos
        import tempfile
        from pathlib import Path as _Path

        with tempfile.TemporaryDirectory() as tmp:
            orig_estado = comandos.ESTADO
            comandos.ESTADO = _Path(tmp) / "comandos_estado.json"
            try:
                # Usuario A fija exito, Usuario B fija alkosto
                comandos.fijar_tienda_activa("exito", chat_id="user_a")
                comandos.fijar_tienda_activa("alkosto", chat_id="user_b")

                self.assertEqual(comandos.tienda_activa(chat_id="user_a"), "exito")
                self.assertEqual(comandos.tienda_activa(chat_id="user_b"), "alkosto")

                # Ofertas mostradas independientes
                comandos.marcar_mostradas(["oferta_1", "oferta_2"], chat_id="user_a")
                comandos.marcar_mostradas(["oferta_3"], chat_id="user_b")

                self.assertIn("oferta_1", comandos.ya_mostradas(chat_id="user_a"))
                self.assertNotIn("oferta_1", comandos.ya_mostradas(chat_id="user_b"))
                self.assertIn("oferta_3", comandos.ya_mostradas(chat_id="user_b"))
            finally:
                comandos.ESTADO = orig_estado

    def test_callback_query_aprobacion_admin(self):
        import server
        from core import telegram, whitelist
        import tempfile
        from pathlib import Path as _Path

        with tempfile.TemporaryDirectory() as tmp:
            orig_wl = whitelist.ARCHIVO_LOCAL
            whitelist.ARCHIVO_LOCAL = _Path(tmp) / "whitelist_test.json"
            orig_admin = config.TELEGRAM_ADMIN_ID
            config.TELEGRAM_ADMIN_ID = "5583002220"

            respuestas_cb = []
            mensajes_editados = []
            mensajes_enviados = []

            orig_resp_cb = telegram.responder_callback
            orig_edit = telegram.editar_mensaje
            orig_send = telegram.send

            telegram.responder_callback = lambda qid, txt="", alerta=False, **k: respuestas_cb.append((qid, txt, alerta)) or True
            telegram.editar_mensaje = lambda chat_id, message_id, texto, reply_markup=None, **k: mensajes_editados.append((chat_id, message_id, texto)) or True
            telegram.send = lambda txt, **k: mensajes_enviados.append((txt, k)) or True

            try:
                # 1. Registrar usuario pendiente
                whitelist.registrar_solicitud("9999", nombre="Luisa", username="luisa99")

                # Intento de aprobacion por un no-admin (debe ser rechazado con alerta)
                cb_no_admin = {
                    "id": "cb_1",
                    "from": {"id": 1111},
                    "data": "aprobar:9999",
                    "message": {"chat": {"id": 1111}, "message_id": 10},
                }
                server.atender_callback_query(cb_no_admin)
                self.assertTrue(any(r[2] is True for r in respuestas_cb))
                self.assertFalse(whitelist.es_permitido("9999"))

                # Intento de aprobacion por el Admin
                cb_admin = {
                    "id": "cb_2",
                    "from": {"id": 5583002220},
                    "data": "aprobar:9999",
                    "message": {"chat": {"id": 5583002220}, "message_id": 20},
                }
                server.atender_callback_query(cb_admin)
                self.assertTrue(whitelist.es_permitido("9999"))
                self.assertTrue(any("Aprobado" in r[1] for r in respuestas_cb))
                self.assertTrue(any("Acceso Autorizado" in m[2] for m in mensajes_editados))
                # Notificacion enviada al usuario con teclado
                self.assertTrue(any(m[1].get("chat_id") == "9999" and "aprobado" in m[0].lower() for m in mensajes_enviados))
            finally:
                whitelist.ARCHIVO_LOCAL = orig_wl
                config.TELEGRAM_ADMIN_ID = orig_admin
                telegram.responder_callback = orig_resp_cb
                telegram.editar_mensaje = orig_edit
                telegram.send = orig_send

    def test_teclado_aprobacion_botones(self):
        from core import telegram
        markup = telegram.teclado_aprobacion(777)
        botones = markup["inline_keyboard"][0]
        self.assertEqual(len(botones), 2)
        self.assertEqual(botones[0]["callback_data"], "aprobar:777")
        self.assertEqual(botones[1]["callback_data"], "rechazar:777")
        self.assertIn("Aprobar", botones[0]["text"])
        self.assertIn("Rechazar", botones[1]["text"])

    def test_inicio_sesion_notifica_al_admin_con_ventana_30_min(self):
        from core import whitelist, telegram
        import radar

        whitelist.limpiar_sesiones()
        orig_admin = config.TELEGRAM_ADMIN_ID
        config.TELEGRAM_ADMIN_ID = "5583002220"

        notificaciones = []
        orig_send = telegram.send
        telegram.send = lambda txt, **k: notificaciones.append((txt, k)) or True

        try:
            # 1. Primera interaccion de usuario aprobado -> Notifica al admin
            solicitud_1 = {
                "comando": "menu",
                "tipo": "menu",
                "chat_id": 4444,
                "user_id": 4444,
                "nombre": "Sofia",
                "username": "sofi_tech",
            }
            radar.atender_solicitudes([solicitud_1])
            notifs_admin = [n for n in notificaciones if n[1].get("chat_id") == "5583002220" and "inició una sesión" in n[0]]
            self.assertEqual(len(notifs_admin), 1)
            self.assertIn("Sofia", notifs_admin[0][0])
            self.assertIn("@sofi_tech", notifs_admin[0][0])

            # 2. Segunda interaccion inmediata (dentro de los 30 min) -> NO repite notificacion
            notificaciones.clear()
            solicitud_2 = {
                "comando": "menu",
                "tipo": "menu",
                "chat_id": 4444,
                "user_id": 4444,
                "nombre": "Sofia",
                "username": "sofi_tech",
            }
            radar.atender_solicitudes([solicitud_2])
            notifs_admin_2 = [n for n in notificaciones if n[1].get("chat_id") == "5583002220" and "inició una sesión" in n[0]]
            self.assertEqual(len(notifs_admin_2), 0)

            # 3. El Admin usando el bot -> NUNCA se notifica a si mismo
            notificaciones.clear()
            solicitud_admin = {
                "comando": "menu",
                "tipo": "menu",
                "chat_id": 5583002220,
                "user_id": 5583002220,
                "nombre": "Emanuel",
            }
            radar.atender_solicitudes([solicitud_admin])
            notifs_admin_self = [n for n in notificaciones if "inició una sesión" in n[0]]
            self.assertEqual(len(notifs_admin_self), 0)

            # 4. Pasan mas de 30 min de inactividad -> Notifica de nuevo
            whitelist._ultimas_sesiones["4444"] -= 1805 # 30 min y 5 seg atras
            notificaciones.clear()
            radar.atender_solicitudes([solicitud_1])
            notifs_admin_3 = [n for n in notificaciones if n[1].get("chat_id") == "5583002220" and "inició una sesión" in n[0]]
            self.assertEqual(len(notifs_admin_3), 1)
        finally:
            config.TELEGRAM_ADMIN_ID = orig_admin
            telegram.send = orig_send
            whitelist.limpiar_sesiones()

    def test_es_miembro_del_canal_estados(self):
        from core import http, telegram
        orig_post = http.post_json
        try:
            # Miembro activo
            http.post_json = lambda url, payload, **k: {"ok": True, "result": {"status": "member"}}
            self.assertTrue(telegram.es_miembro_del_canal(12345, channel_id="-100"))

            # Administrador o creador
            http.post_json = lambda url, payload, **k: {"ok": True, "result": {"status": "administrator"}}
            self.assertTrue(telegram.es_miembro_del_canal(12345, channel_id="-100"))

            # Salio del canal
            http.post_json = lambda url, payload, **k: {"ok": True, "result": {"status": "left"}}
            self.assertFalse(telegram.es_miembro_del_canal(12345, channel_id="-100"))

            # Baneado
            http.post_json = lambda url, payload, **k: {"ok": True, "result": {"status": "kicked"}}
            self.assertFalse(telegram.es_miembro_del_canal(12345, channel_id="-100"))

            # Error de API o no encontrado
            http.post_json = lambda url, payload, **k: {"ok": False, "description": "USER_NOT_PARTICIPANT"}
            self.assertFalse(telegram.es_miembro_del_canal(12345, channel_id="-100"))
        finally:
            http.post_json = orig_post

    def test_teclado_unirse_canal_estructura(self):
        from core import telegram
        teclado = telegram.teclado_unirse_canal("https://t.me/test_canal")
        filas = teclado["inline_keyboard"]
        self.assertEqual(len(filas), 2)
        self.assertEqual(filas[0][0]["url"], "https://t.me/test_canal")
        self.assertEqual(filas[1][0]["callback_data"], "verificar_canal")

    def test_callback_verificar_canal_flujo(self):
        import server
        from core import telegram, whitelist
        import tempfile
        from pathlib import Path as _Path

        with tempfile.TemporaryDirectory() as tmp:
            orig_wl = whitelist.ARCHIVO_LOCAL
            whitelist.ARCHIVO_LOCAL = _Path(tmp) / "whitelist_test.json"
            orig_admin = config.TELEGRAM_ADMIN_ID
            config.TELEGRAM_ADMIN_ID = "5583002220"

            respuestas_cb = []
            mensajes_enviados = []
            mensajes_editados = []

            orig_resp = telegram.responder_callback
            orig_send = telegram.send
            orig_edit = telegram.editar_mensaje
            orig_miembro = telegram.es_miembro_del_canal

            telegram.responder_callback = lambda qid, txt="", alerta=False, **k: respuestas_cb.append((qid, txt, alerta)) or True
            telegram.send = lambda txt, **k: mensajes_enviados.append((txt, k)) or True
            telegram.editar_mensaje = lambda chat_id, message_id, texto, **k: mensajes_editados.append((chat_id, message_id, texto)) or True

            try:
                cb_user = {
                    "id": "cb_verif",
                    "from": {"id": 3333, "first_name": "Andres", "username": "andres33"},
                    "data": "verificar_canal",
                    "message": {"chat": {"id": 3333}, "message_id": 55},
                }

                # 1. Aun no se ha unido al canal -> Alerta emergente
                telegram.es_miembro_del_canal = lambda uid, **k: False
                server.atender_callback_query(cb_user)
                self.assertTrue(any(r[2] is True for r in respuestas_cb))
                self.assertEqual(len(mensajes_enviados), 0)

                # 2. Ya se unio pero no esta en whitelist -> Alerta confirmada, notifica al admin para aprobar
                respuestas_cb.clear()
                telegram.es_miembro_del_canal = lambda uid, **k: True
                server.atender_callback_query(cb_user)
                self.assertTrue(any("Confirmado" in r[1] for r in respuestas_cb))
                self.assertTrue(any(m[1].get("chat_id") == "5583002220" and "Unido a Ofertas" in m[0] for m in mensajes_enviados))

                # 3. Ya se unio Y esta en whitelist -> Despliega menu directamente
                whitelist.aprobar("3333")
                mensajes_enviados.clear()
                server.atender_callback_query(cb_user)
                self.assertTrue(any(m[1].get("chat_id") == 3333 and "Menú Principal" in m[0] for m in mensajes_enviados))
            finally:
                whitelist.ARCHIVO_LOCAL = orig_wl
                config.TELEGRAM_ADMIN_ID = orig_admin
                telegram.responder_callback = orig_resp
                telegram.send = orig_send
                telegram.editar_mensaje = orig_edit
                telegram.es_miembro_del_canal = orig_miembro



class PruebaNuevasTiendasYCategoriasEspecificas(unittest.TestCase):
    """Verifica que las tiendas VTEX agregadas y las cositas especificas funcionen."""

    def test_tiendas_vtex_nuevas_registradas(self):
        from sources import vtex
        from core import comandos
        tiendas_esperadas = ["totto", "studiof", "velez", "americanino", "arturocalle"]
        for t in tiendas_esperadas:
            self.assertIn(t, vtex.TIENDAS)
            self.assertIn(t, comandos.CATALOGO)
            self.assertEqual(comandos.CATALOGO[t][0], "vtex")

    def test_botones_tiendas_nuevas(self):
        from core import comandos
        original = config.TELEGRAM_CHAT_ID
        config.TELEGRAM_CHAT_ID = "-100"
        try:
            def parsear(txt):
                return comandos.leer_comando({"text": txt, "chat": {"id": -100}})

            self.assertEqual(parsear("🎒 Totto")["tienda"], "totto")
            self.assertEqual(parsear("👗 Studio F")["tienda"], "studiof")
            self.assertEqual(parsear("👞 Vélez")["tienda"], "velez")
            self.assertEqual(parsear("🦅 Americanino")["tienda"], "americanino")
            self.assertEqual(parsear("👔 Arturo Calle")["tienda"], "arturocalle")
        finally:
            config.TELEGRAM_CHAT_ID = original

    def test_grupos_de_productos_cotidianos(self):
        from core import comandos
        original = config.TELEGRAM_CHAT_ID
        config.TELEGRAM_CHAT_ID = "-100"
        try:
            def parsear(txt):
                return comandos.leer_comando({"text": txt, "chat": {"id": -100}})

            res_cocina = parsear("🍳 Cocina")
            self.assertEqual(res_cocina["tipo"], "grupo_categoria")
            self.assertEqual(res_cocina["grupo"], "cocina")

            res_tec = parsear("💻 Tecnología")
            self.assertEqual(res_tec["tipo"], "grupo_categoria")
            self.assertEqual(res_tec["grupo"], "tecnologia")

            res_nev = parsear("❄️ Neveras y Lavadoras")
            self.assertEqual(res_nev["tipo"], "grupo_categoria")
            self.assertEqual(res_nev["grupo"], "neveras")

            res_ropa = parsear("👟 Ropa y Tenis")
            self.assertEqual(res_ropa["tipo"], "grupo_categoria")
            self.assertEqual(res_ropa["grupo"], "ropa")

            res_hogar = parsear("🏠 Hogar")
            self.assertEqual(res_hogar["tipo"], "grupo_categoria")
            self.assertEqual(res_hogar["grupo"], "hogar")

            res_volver = parsear("⬅️ Volver a Grupos")
            self.assertEqual(res_volver["tipo"], "grupo_categoria")
            self.assertEqual(res_volver["grupo"], "volver")
        finally:
            config.TELEGRAM_CHAT_ID = original

    def test_cositas_especificas_consultas(self):
        from core import comandos
        original = config.TELEGRAM_CHAT_ID
        config.TELEGRAM_CHAT_ID = "-100"
        try:
            def parsear(txt):
                return comandos.leer_comando({"text": txt, "chat": {"id": -100}})

            # Cocina: Airfryers y Sandwicheras
            r_air = parsear("🍟 Airfryers")
            self.assertEqual(r_air["tipo"], "categoria")
            self.assertIn("freidora de aire", r_air["consultas"])

            r_sand = parsear("🥪 Sandwicheras")
            self.assertEqual(r_sand["tipo"], "categoria")
            self.assertIn("sandwichera", r_sand["consultas"])

            # Neveras y Lavadoras
            r_nev = parsear("❄️ Neveras")
            self.assertEqual(r_nev["tipo"], "categoria")
            self.assertIn("nevera", r_nev["consultas"])

            r_lav = parsear("🧺 Lavadoras")
            self.assertEqual(r_lav["tipo"], "categoria")
            self.assertIn("lavadora", r_lav["consultas"])

            # Calzado
            r_zap = parsear("👟 Tenis y Zapatos")
            self.assertEqual(r_zap["tipo"], "categoria")
            self.assertIn("tenis", r_zap["consultas"])

            # Hogar
            r_asp = parsear("🧹 Aspiradoras")
            self.assertEqual(r_asp["tipo"], "categoria")
            self.assertIn("aspiradora", r_asp["consultas"])
        finally:
            config.TELEGRAM_CHAT_ID = original

    def test_teclados_especificos_bien_formados(self):
        from core import telegram
        # Teclado tiendas incluye las nuevas
        t_tiendas = telegram.teclado_tiendas()
        textos_tiendas = [b["text"] for row in t_tiendas["keyboard"] for b in row]
        self.assertIn("🎒 Totto", textos_tiendas)
        self.assertIn("👗 Studio F", textos_tiendas)
        self.assertIn("👞 Vélez", textos_tiendas)
        self.assertIn("🦅 Americanino", textos_tiendas)
        self.assertIn("👔 Arturo Calle", textos_tiendas)

        # Tienda de ropa despliega moda directamente
        t_totto = telegram.teclado_categorias("Totto")
        textos_totto = [b["text"] for row in t_totto["keyboard"] for b in row]
        self.assertIn("👟 Tenis y Zapatos", textos_totto)
        self.assertIn("👕 Camisetas y Polos", textos_totto)

        # Teclado Cocina
        t_cocina = telegram.teclado_cocina()
        textos_cocina = [b["text"] for row in t_cocina["keyboard"] for b in row]
        self.assertIn("🍟 Airfryers", textos_cocina)
        self.assertIn("🥪 Sandwicheras", textos_cocina)
        self.assertIn("🍹 Licuadoras", textos_cocina)

        # Teclado Tecnologia
        t_tec = telegram.teclado_tecnologia()
        textos_tec = [b["text"] for row in t_tec["keyboard"] for b in row]
        self.assertIn("📺 Televisores", textos_tec)
        self.assertIn("💻 Portátiles", textos_tec)
        self.assertIn("📱 Celulares", textos_tec)

        # Tiendas de electrodomésticos en teclado_tiendas
        self.assertIn("🟢 Jumbo", textos_tiendas)
        self.assertIn("🔵 Alkomprar", textos_tiendas)
        self.assertIn("🔴 Haceb", textos_tiendas)
        self.assertIn("🌀 Whirlpool", textos_tiendas)
        self.assertIn("🍳 Imusa", textos_tiendas)
        self.assertIn("☕ Oster", textos_tiendas)

        # Enrutamiento inteligente a teclados de cocina y línea blanca
        t_imusa = telegram.teclado_categorias("Imusa")
        textos_imusa = [b["text"] for row in t_imusa["keyboard"] for b in row]
        self.assertIn("🍟 Airfryers", textos_imusa)
        self.assertIn("🥪 Sandwicheras", textos_imusa)

        t_haceb = telegram.teclado_categorias("Haceb")
        textos_haceb = [b["text"] for row in t_haceb["keyboard"] for b in row]
        self.assertIn("❄️ Neveras", textos_haceb)
        self.assertIn("🧺 Lavadoras", textos_haceb)

    def test_tiendas_electrodomesticos_registradas(self):
        from sources import vtex, algolia_co
        from core import comandos

        for t in ["jumbo", "haceb", "whirlpool", "imusa", "oster"]:
            self.assertIn(t, vtex.TIENDAS)
            self.assertIn(t, comandos.CATALOGO)
            self.assertEqual(comandos.CATALOGO[t][0], "vtex")

        self.assertIn("alkomprar", algolia_co.TIENDAS)
        self.assertIn("alkomprar", comandos.CATALOGO)
        self.assertEqual(comandos.CATALOGO["alkomprar"][0], "algolia_co")

        original = config.TELEGRAM_CHAT_ID
        config.TELEGRAM_CHAT_ID = "-100"
        try:
            def parsear(txt):
                return comandos.leer_comando({"text": txt, "chat": {"id": -100}})

            self.assertEqual(parsear("🟢 Jumbo")["tienda"], "jumbo")
            self.assertEqual(parsear("🔵 Alkomprar")["tienda"], "alkomprar")
            self.assertEqual(parsear("🔴 Haceb")["tienda"], "haceb")
            self.assertEqual(parsear("🌀 Whirlpool")["tienda"], "whirlpool")
            self.assertEqual(parsear("🍳 Imusa")["tienda"], "imusa")
            self.assertEqual(parsear("☕ Oster")["tienda"], "oster")
            self.assertEqual(parsear("🇨🇴 Comparar Tiendas")["tienda"], "colombia")
            self.assertEqual(parsear("🇨🇴 Todo Colombia")["tienda"], "colombia")
        finally:
            config.TELEGRAM_CHAT_ID = original

    def test_teclado_tiendas_incluye_comparar_tiendas(self):
        from core import telegram
        t = telegram.teclado_tiendas()
        textos = [b["text"] for row in t["keyboard"] for b in row]
        self.assertIn("🇨🇴 Comparar Tiendas", textos)

    def test_balance_top3_por_tienda(self):
        import radar
        from core.models import Deal
        from unittest.mock import patch

        alkosto_deals = [
            Deal("algolia_co", "Alkosto", "CO", f"k:alk:{i}", f"Nevera Modelo {i} Alkosto", "http://a", 100 + i * 10, "COP", 250, in_stock=True)
            for i in range(5)
        ]
        exito_deals = [
            Deal("vtex", "Exito", "CO", f"k:exi:{i}", f"Refrigerador Serie {i} Exito", "http://e", 120 + i * 10, "COP", 250, in_stock=True)
            for i in range(4)
        ]
        haceb_deals = [
            Deal("vtex", "Haceb", "CO", f"k:hac:{i}", f"Nevecon Linea {i} Haceb", "http://h", 150 + i * 10, "COP", 250, in_stock=True)
            for i in range(2)
        ]

        def _mock_ofertas(_wl, fuente, _tiendas, **_kw):
            if fuente == "algolia_co":
                return alkosto_deals
            elif fuente == "vtex":
                return exito_deals + haceb_deals
            return []

        with patch.object(radar, "_ofertas_de", side_effect=_mock_ofertas):
            seleccion = radar._mejores_colombia({}, set(), cuantas=15, consultas_custom=["nevera", "refrigerador", "nevecon"], max_por_tienda=3)
            conteo = {}
            for deal, _ in seleccion:
                conteo[deal.store] = conteo.get(deal.store, 0) + 1

            self.assertEqual(conteo.get("Alkosto"), 3)
            self.assertEqual(conteo.get("Exito"), 3)
            self.assertEqual(conteo.get("Haceb"), 2)
            self.assertEqual(len(seleccion), 8)

    def test_filtro_accesorios_bloquea_compresor(self):
        from core import filtros
        self.assertTrue(filtros.es_accesorio("Compresor GMCC 1/5 HP para Nevera"))
        self.assertTrue(filtros.es_accesorio("Motor ventilador para lavadora"))
        self.assertTrue(filtros.es_accesorio("Termostato para nevera Haceb"))
        # Un producto completo que menciona compresor en su descripción no se descarta
        self.assertFalse(filtros.es_accesorio("Nevera Haceb No Frost 300L con compresor inverter"))

    def test_menciona_singular_y_plural(self):
        from core import filtros
        self.assertTrue(filtros.menciona("Pack x3 Camisetas Hombre", "camiseta"))
        self.assertTrue(filtros.menciona("Pantalones de Vestir", "pantalon"))
        self.assertTrue(filtros.menciona("Neveras No Frost", "nevera"))
        self.assertFalse(filtros.menciona("Pelota Teknofit", "camiseta"))
        self.assertFalse(filtros.menciona("Llanta Michelin", "camiseta"))
        self.assertFalse(filtros.menciona("Jean Unicolor", "camiseta"))

    def test_categoria_descarta_productos_irrelevantes(self):
        import radar
        from core.models import Deal
        from unittest.mock import patch

        ofertas_variadas = [
            Deal("algolia_co", "Alkosto", "CO", "k1", "Pelota Teknofit", "http://a", 50, "COP", 100, in_stock=True),
            Deal("algolia_co", "Alkosto", "CO", "k2", "Llanta Michelin 185", "http://a", 200, "COP", 400, in_stock=True),
            Deal("vtex", "Arturo Calle", "CO", "k3", "Jean Unicolor", "http://ac", 40, "COP", 100, in_stock=True),
            Deal("vtex", "Totto", "CO", "k4", "Camiseta Manga Corta", "http://to", 30, "COP", 100, in_stock=True),
            Deal("vtex", "Falabella", "CO", "k5", "Polo Clásica Algodón", "http://fa", 25, "COP", 80, in_stock=True),
        ]

        with patch.object(radar, "_ofertas_de", return_value=ofertas_variadas):
            res = radar._mejores_colombia({}, set(), cuantas=10, consultas_custom=["camiseta", "polo", "camisa"], max_por_tienda=3)
            titulos = [d.title for d, _ in res]
            self.assertIn("Camiseta Manga Corta", titulos)
            self.assertIn("Polo Clásica Algodón", titulos)
            self.assertNotIn("Pelota Teknofit", titulos)
            self.assertNotIn("Llanta Michelin 185", titulos)
            self.assertNotIn("Jean Unicolor", titulos)


if __name__ == "__main__":
    unittest.main(verbosity=2)



