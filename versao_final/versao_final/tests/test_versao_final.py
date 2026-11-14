import os
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from bandejas import SistemaBandejas, SETORES_ATENDIMENTO, RegraNegocioError

class VersaoFinalTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'bandejas_web.db'
        self.s=SistemaBandejas(self.path)

    def test_trinta_kits_arsenal_sem_setor(self):
        kits=self.s.listar_bandejas()
        self.assertEqual(len(kits),30)
        self.assertTrue(all(k['tipo']=='Arsenal' and k['localizacao_atual']=='Arsenal' and k['etapa']=='estoque' for k in kits))
        self.assertEqual(len({(k['cor_1'],k['cor_2']) for k in kits}),3)
        self.assertEqual(sorted([sum(1 for k in kits if (k['cor_1'],k['cor_2'])==p) for p in {(k['cor_1'],k['cor_2']) for k in kits}]),[10,10,10])
        self.assertTrue(all(sum(i['quantidade'] for i in b['itens'])==13 for b in kits))

    def test_multissetorial_sem_reserva_de_bandejas(self):
        g=self.s.cadastrar_usuario('Gerente','','gerente_estoque')
        padroes=self.s.resumo_padroes_por_setor()
        self.assertEqual(len(padroes),len(SETORES_ATENDIMENTO)*3)
        self.assertEqual({p['setor'] for p in padroes},set(SETORES_ATENDIMENTO))
        padrao=padroes[0]
        codigo=next(k['codigo'] for k in self.s.listar_bandejas() if (k['cor_1'],k['cor_2'])==(padrao['cor_1'],padrao['cor_2']))
        m=self.s.cadastrar_usuario('Medico','CTI','medico',g)
        t=self.s.cadastrar_usuario('Tecnico','CTI','enfermeiro',g)
        pedido=self.s.solicitar(m,'CTI',padrao['padrao_id'])
        self.s.separar_estoque(g,pedido['codigo_solicitacao'],codigo)
        self.assertEqual(next(x for x in self.s.listar_bandejas() if x['codigo']==codigo)['localizacao_atual'],'Em transporte')
        paciente=self.s.listar_pacientes(t)[0]
        self.s.checkin_pedido(t,pedido['codigo_solicitacao'],codigo,paciente_id=paciente['id'])
        self.assertEqual(next(x for x in self.s.listar_bandejas() if x['codigo']==codigo)['localizacao_atual'],'CTI')
        self.s.checkout(t,codigo,'CTI')
        self.s.receber_emprestimo(t,codigo)
        self.s.enviar_estoque(t,codigo)
        self.s.receber_estoque(g,codigo)
        devolvida=next(x for x in self.s.listar_bandejas() if x['codigo']==codigo)
        self.assertEqual((devolvida['localizacao_atual'],devolvida['tipo'],devolvida['etapa']),('Arsenal','Arsenal','estoque'))
        # Mesmo kit agora pode ir à Enfermaria.
        m2=self.s.cadastrar_usuario('Medica','Enfermaria','medico',g)
        pedido2=self.s.solicitar(m2,'Enfermaria',padrao['padrao_id'])
        self.s.separar_estoque(g,pedido2['codigo_solicitacao'],codigo)
        self.assertEqual(next(x for x in self.s.listar_bandejas() if x['codigo']==codigo)['localizacao_atual'],'Em transporte')

    def test_nao_permite_escolha_tipo_alheio_e_sem_cores(self):
        g=self.s.cadastrar_usuario('Gerente','','gerente_estoque')
        m=self.s.cadastrar_usuario('Medica','CTI','medico',g)
        p=self.s.resumo_padroes_por_setor()[0]['padrao_id']
        with self.assertRaises(RegraNegocioError):self.s.solicitar(m,'Enfermaria',p)
        with self.assertRaises(RegraNegocioError):self.s.solicitar(m,'CTI')

    def test_reabertura_nao_gera_duplicacao(self):
        SistemaBandejas(self.path)
        self.assertEqual(len(self.s.listar_bandejas()),30)

    def test_migracao_preserva_base_anterior(self):
        # Recria uma base legada com o código da versão anterior, em arquivo temporário.
        import importlib.util
        origem=Path(__file__).resolve().parents[2]/'traytrack_v10_4_setores'/'bandejas.py'
        spec=importlib.util.spec_from_file_location('base_antiga_para_teste',origem)
        mod=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        antigo=Path(self.tmp.name)/'antigo.db'
        a=mod.SistemaBandejas(antigo)
        self.assertEqual(len(a.listar_bandejas()),40)
        g=a.cadastrar_usuario('Gestor','','gerente_estoque')
        m=a.cadastrar_usuario('Medico','CTI','medico',g)
        pedido=a.solicitar(m,'CTI')
        shutil.copyfile(antigo,self.path)
        novo=SistemaBandejas(self.path)
        self.assertEqual(len(novo.listar_bandejas()),40)
        self.assertEqual(len(novo.listar_usuarios()),2)
        self.assertEqual(len(novo.listar_pedidos()),1)
        self.assertTrue(all(b['localizacao_atual']=='Arsenal' and b['tipo']=='Arsenal' for b in novo.listar_bandejas()))

if __name__=='__main__':unittest.main()
