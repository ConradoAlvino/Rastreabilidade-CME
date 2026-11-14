import os
import tempfile
import unittest
from pathlib import Path
from bandejas import SistemaBandejas

class InterfaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory()
        os.environ['BANDEJAS_DB']=str(Path(cls.temp.name)/'bandejas_web.db')
        import web_app
        cls.web_app=web_app

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_checklist_completa_e_pre_marcada(self):
        web_app=self.web_app
        b=web_app.ENGINE.listar_bandejas()[0]
        html=web_app.checklist_fields(b)
        self.assertIn('Gaze (pacote) Unidade 1 presente',html)
        self.assertIn('Gaze (pacote) Unidade 2 presente',html)
        self.assertIn('Observações da conferência',html)
        self.assertIn('Lacre violado',html)
        self.assertEqual(html.count(' checked>'),13)
        self.assertIn('name="foto"',html)

    def test_gerente_ve_catalogo_sem_duplicar_setores(self):
        web_app=self.web_app
        html=web_app.catalogo_medico()
        self.assertIn('Arsenal',html)
        self.assertEqual(html.count('Todos os setores'),3)

if __name__=='__main__':unittest.main()
