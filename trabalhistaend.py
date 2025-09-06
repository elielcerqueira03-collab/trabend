import streamlit as st
import os
import requests
import asyncio
import pandas as pd
import time
import json
import base64
from camoufox.async_api import AsyncCamoufox
from io import BytesIO

# ==============================================================================
# CONFIGURAÇÃO DA PÁGINA STREAMLIT
# ==============================================================================

st.set_page_config(
    page_title="Extrator de Processos PJe",
    page_icon="⚖️",
    layout="wide",
    initial_sidebar_state="expanded"
)

# ==============================================================================
# CLASSE DE AUTOMAÇÃO DE LOGIN (Estrutura estável, com logging para o Streamlit)
# ==============================================================================

class PjeLoginAutomation:
    def __init__(self, trt_number: int, status_placeholder):
        self.trt_number = trt_number
        self.instancia_login = "primeirograu"
        self.status_placeholder = status_placeholder

    def log(self, message):
        """Função para exibir logs na interface do Streamlit."""
        self.status_placeholder.text(message)
        print(message) # Mantém o log no console também

    def decode_jwt_payload(self, token: str):
        try:
            payload_b64 = token.split('.')[1]
            payload_b64 += '=' * (-len(payload_b64) % 4)
            decoded_payload = base64.b64decode(payload_b64).decode('utf-8')
            return json.loads(decoded_payload)
        except Exception:
            return None

    async def perform_browser_login(self, page, username, password):
        self.log(f"🚀 Iniciando login via navegador no PJE TRT{self.trt_number}...")
        try:
            await page.context.clear_cookies()
            login_url = f"https://pje.trt{self.trt_number}.jus.br/{self.instancia_login}/login.seam"
            await page.goto(login_url, timeout=60000)
            await page.wait_for_load_state('networkidle', timeout=30000)

            is_pdpj_flow = await page.is_visible("#btnSsoPdpj", timeout=7000)

            if is_pdpj_flow:
                self.log("-> Detectado fluxo de login via PDPJ.")
                await page.click("#btnSsoPdpj")
                await page.wait_for_url("**/sso.cloud.pje.jus.br/**", timeout=20000)
                await page.fill("#username", username)
                await page.fill("#password", password)
                await page.click("#kc-login")
            else:
                self.log("-> Utilizando fluxo de login tradicional do PJe.")
                await page.click("text=Entrar com CPF ou OAB", timeout=15000)
                await page.wait_for_selector("#username", timeout=15000)
                await page.fill("#username", username)
                await page.fill("#password", password)
                await page.click("input[name='login:btnEntrar']")

            await page.wait_for_url(f"https://pje.trt{self.trt_number}.jus.br/pjekz/painel/usuario-externo*", timeout=30000)
            self.log("✅ Login via navegador bem-sucedido. Coletando tokens...")
            
            all_cookies = await page.context.cookies()
            cookie_string = "; ".join([f"{c['name']}={c['value']}" for c in all_cookies])
            xsrf_token = next((c['value'] for c in all_cookies if c['name'] == 'Xsrf-Token'), None)
            access_token = next((c['value'] for c in all_cookies if c['name'] == 'access_token'), None)

            if not xsrf_token or not access_token:
                self.log("❌ Falha ao obter XSRF Token ou Access Token.")
                return None
                
            token_payload = self.decode_jwt_payload(access_token)
            id_painel = token_payload.get('id') if token_payload else None

            if not id_painel:
                self.log("❌ Falha ao decodificar o ID do painel a partir do token.")
                return None
            
            self.log(f"-> ID do Painel para o TRT-{self.trt_number} detectado: {id_painel}")
            return {'cookie': cookie_string, 'xsrf_token': xsrf_token, 'id_painel': id_painel}

        except Exception as e:
            self.log(f"❌ Erro fatal durante o login no TRT{self.trt_number}: {e}")
            return None

# ==============================================================================
# FUNÇÃO DE EXTRAÇÃO DE DADOS (Com logging para o Streamlit)
# ==============================================================================

def extract_pje_data(auth_tokens: dict, trt_number: int, tipo_painel: int, nome_painel: str, status_placeholder):
    id_advogado = auth_tokens['id_painel']
    domain = f"pje.trt{trt_number}.jus.br"
    base_url = f"https://{domain}/pje-comum-api/api/paineladvogado/{id_advogado}/processos"
    processos_deste_trt = []
    pagina_atual = 1
    
    status_placeholder.text(f"📊 Iniciando extração de '{nome_painel}' do TRT-{trt_number}...")
    
    headers = {
        'Accept': 'application/json, text/plain, */*', 'Cookie': auth_tokens['cookie'],
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36 Edg/139.0.0.0',
        'X-Xsrf-Token': auth_tokens['xsrf_token'], 'Referer': f'https://{domain}/pjekz/painel/usuario-externo/advogado'
    }

    while True:
        params = {
            'pagina': pagina_atual, 'tamanhoPagina': 100, 'tipoPainelAdvogado': tipo_painel,
            'ordenacaoCrescente': 'false', 'data': int(time.time() * 1000)
        }
        try:
            response = requests.get(base_url, params=params, headers=headers, timeout=30)
            if response.status_code == 200:
                dados = response.json()
                processos_da_pagina = dados.get('resultado', [])
                if not processos_da_pagina:
                    status_placeholder.text(f"✅ Fim da extração para '{nome_painel}' do TRT-{trt_number}.")
                    break
                processos_deste_trt.extend(processos_da_pagina)
                status_placeholder.text(f"-> Página {pagina_atual} de '{nome_painel}' extraída. Total para TRT-{trt_number}: {len(processos_deste_trt)}")
                pagina_atual += 1
                time.sleep(1.5)
            else:
                status_placeholder.text(f"❌ Erro na página {pagina_atual} (Status: {response.status_code}). Parando extração para TRT-{trt_number}.")
                break
        except requests.exceptions.RequestException as e:
            status_placeholder.text(f"❌ Erro de conexão na página {pagina_atual} para TRT-{trt_number}: {e}")
            break
    return processos_deste_trt

# ==============================================================================
# FUNÇÃO DE FORMATAÇÃO DA PLANILHA (AJUSTADA)
# ==============================================================================

def formatar_dataframe(df: pd.DataFrame, tipo_extracao: str):
    """Aplica formatação de datas e renomeia colunas para o DataFrame final."""
    st.info(f"✨ Formatando dados para a aba '{tipo_extracao}'...")

    # Correção: Remove a coluna de data de arquivamento se a extração for do acervo geral
    if tipo_extracao == 'Acervo Geral' and 'dataArquivamento' in df.columns:
        df = df.drop(columns=['dataArquivamento'])

    if 'dataAutuacao' in df.columns:
        df['dataAutuacao'] = pd.to_datetime(df['dataAutuacao'], errors='coerce').dt.strftime('%d/%m/%Y')
    if 'dataArquivamento' in df.columns:
        df['dataArquivamento'] = pd.to_datetime(df['dataArquivamento'], errors='coerce').dt.strftime('%d/%m/%Y')
    if 'dataProximaAudiencia' in df.columns:
        df['dataProximaAudiencia'] = pd.to_datetime(df['dataProximaAudiencia'], errors='coerce').dt.strftime('%d/%m/%Y %H:%M')

    de_para_colunas = {
        'TRT': 'Código Tribunal', 'id': 'ID do Processo', 'descricaoOrgaoJulgador': 'Órgão Julgador',
        'classeJudicial': 'Classe Processual', 'numero': 'Número', 'numeroProcesso': 'Número Completo do Processo',
        'segredoDeJustica': 'Segredo de Justiça', 'codigoStatusProcesso': 'Status', 'prioridadeProcessual': 'Prioridade',
        'nomeParteAutora': 'Parte Autora', 'qtdeParteAutora': 'Qtd. Autores', 'nomeParteRe': 'Parte Ré',
        'registroComplementarParteRe': 'Registro Comp. Ré', 'qtdeParteRe': 'Qtd. Rés', 'dataAutuacao': 'Data de Autuação',
        'juizoDigital': 'Juízo 100% Digital', 'dataArquivamento': 'Data de Arquivamento', 'temAssociacao': 'Possui Associação',
        'registroComplementarParteAutora': 'Registro Comp. Autora', 'dataProximaAudiencia': 'Próxima Audiência'
    }
    df = df.rename(columns=de_para_colunas)
    
    # Reorganiza as colunas, se 'Código Tribunal' existir
    if 'Código Tribunal' in df.columns:
        cols = ['Código Tribunal'] + [col for col in df.columns if col != 'Código Tribunal']
        df = df[cols]
    return df

# ==============================================================================
# INTERFACE E LÓGICA PRINCIPAL DO STREAMLIT
# ==============================================================================

st.title("⚖️ Extrator de Processos PJe")
st.markdown("---")

# --- BARRA LATERAL DE CONFIGURAÇÃO ---
with st.sidebar:
    st.header("⚙️ Configurações")
    
    pje_username = st.text_input("👤 Usuário PJe (CPF)", value="02200568541")
    pje_password = st.text_input("🔑 Senha PJe", type="password", value="Lop@2025")
    
    st.markdown("---")
    
    tipo_extracao_options = {
        "Apenas Acervo Geral": ("acervo", "processos_acervo_geral.xlsx"),
        "Apenas Processos Arquivados": ("arquivados", "processos_arquivados.xlsx"),
        "Ambos (Gera duas abas)": ("ambos", "processos_geral_e_arquivados.xlsx")
    }
    
    escolha_extracao = st.radio(
        "Selecione o tipo de extração:",
        options=tipo_extracao_options.keys()
    )
    
    st.markdown("---")
    
    todos_trts = list(range(1, 25))
    trts_excluidos = st.multiselect(
        "Selecione os TRTs para EXCLUIR da busca:",
        options=todos_trts,
        help="Deixe em branco para buscar em todos os 24 TRTs."
    )
    
    trts_a_processar = [trt for trt in todos_trts if trt not in trts_excluidos]
    st.info(f"Serão processados **{len(trts_a_processar)}** TRTs.")

# --- ÁREA PRINCIPAL ---
col1, col2 = st.columns([3, 1])

with col1:
    st.subheader("Painel de Controle")
    iniciar_button = st.button("▶️ Iniciar Extração", type="primary", use_container_width=True)

with col2:
    st.subheader("Instância PJe")
    instancia_api = st.selectbox("API", ["pjekz"], disabled=True)
    instancia_login = st.selectbox("Login", ["primeirograu"], disabled=True)


if iniciar_button:
    if not pje_username or not pje_password:
        st.error("❌ Por favor, preencha o usuário e a senha do PJe.")
    else:
        # Define o que será extraído com base na escolha
        escolha_codigo, nome_arquivo_final = tipo_extracao_options[escolha_extracao]
        trabalhar_com_acervo = escolha_codigo in ["acervo", "ambos"]
        trabalhar_com_arquivados = escolha_codigo in ["arquivados", "ambos"]
        
        # Elementos para exibir o status e progresso
        progress_bar = st.progress(0, text="Aguardando início...")
        status_placeholder = st.empty()
        
        lista_acervo_geral = []
        lista_arquivados = []

        async def run_extraction():
            async with AsyncCamoufox(headless=True) as browser:
                total_trts = len(trts_a_processar)
                for i, trt_atual in enumerate(trts_a_processar):
                    progress_text = f"Processando TRT {trt_atual} de {trts_a_processar[-1]}... ({i+1}/{total_trts})"
                    progress_bar.progress((i + 1) / total_trts, text=progress_text)
                    
                    pje_automation = PjeLoginAutomation(trt_number=trt_atual, status_placeholder=status_placeholder)
                    page = await browser.new_page(no_viewport=True)
                    auth_tokens = None
                    try:
                        auth_tokens = await pje_automation.perform_browser_login(page, pje_username, pje_password)
                    finally:
                        await page.close()

                    if auth_tokens:
                        if trabalhar_com_acervo:
                            processos_acervo = extract_pje_data(auth_tokens, trt_atual, 1, "Acervo Geral", status_placeholder)
                            if processos_acervo:
                                for p in processos_acervo: p['TRT'] = trt_atual
                                lista_acervo_geral.extend(processos_acervo)

                        if trabalhar_com_arquivados:
                            processos_arquivados = extract_pje_data(auth_tokens, trt_atual, 5, "Processos Arquivados", status_placeholder)
                            if processos_arquivados:
                                for p in processos_arquivados: p['TRT'] = trt_atual
                                lista_arquivados.extend(processos_arquivados)
                    else:
                        status_placeholder.text(f"❌ FALHA na autenticação para o TRT-{trt_atual}. Pulando.")
                    time.sleep(2)

            # --- Geração da Planilha e Download ---
            st.markdown("---")
            st.subheader("🏁 Processamento Finalizado!")

            if not lista_acervo_geral and not lista_arquivados:
                st.warning("Nenhum processo foi coletado no total.")
                return

            status_placeholder.text(f"💾 Gerando planilha consolidada '{nome_arquivo_final}'...")
            output = BytesIO()
            with pd.ExcelWriter(output, engine='openpyxl') as writer:
                if lista_acervo_geral:
                    st.write(f"-> Processando **{len(lista_acervo_geral)}** registros do Acervo Geral...")
                    df_acervo = pd.DataFrame(lista_acervo_geral)
                    df_acervo_formatado = formatar_dataframe(df_acervo, "Acervo Geral")
                    df_acervo_formatado.to_excel(writer, sheet_name='Acervo Geral', index=False)
                    st.success("-> Aba 'Acervo Geral' adicionada.")

                if lista_arquivados:
                    st.write(f"-> Processando **{len(lista_arquivados)}** registros dos Arquivados...")
                    df_arquivados = pd.DataFrame(lista_arquivados)
                    df_arquivados_formatado = formatar_dataframe(df_arquivados, "Processos Arquivados")
                    df_arquivados_formatado.to_excel(writer, sheet_name='Processos Arquivados', index=False)
                    st.success("-> Aba 'Processos Arquivados' adicionada.")
            
            output.seek(0)
            st.balloons()
            st.download_button(
                label=f"📥 Baixar Planilha ({nome_arquivo_final})",
                data=output,
                file_name=nome_arquivo_final,
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True
            )

        # Executa a função assíncrona
        asyncio.run(run_extraction())