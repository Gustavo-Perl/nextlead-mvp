import streamlit as st
import pandas as pd
import json
import io
import requests
import unicodedata
import re
import concurrent.futures
from openai import OpenAI
from duckduckgo_search import DDGS 

# =============================================================================
# 0. FORMATADORES E GERADORES DE FICHEIROS
# =============================================================================
def formatar_nome_linkedin(nome):
    """Remove acentos e formata para o padrão da URL do LinkedIn."""
    nome_sem_acento = ''.join(
        c for c in unicodedata.normalize('NFD', nome)
        if unicodedata.category(c) != 'Mn'
    )
    nome_limpo = re.sub(r'[^a-z0-9 ]', '', nome_sem_acento.lower())
    return re.sub(r'\s+', '-', nome_limpo.strip())

def formatar_faturamento(valor):
    """Converte números absolutos extensos num formato legível em Reais."""
    valor_str = str(valor).strip()
    if re.fullmatch(r'\d+', valor_str.replace('.', '').replace(',', '')):
        try:
            num = float(valor_str.replace(',', ''))
            if num >= 1_000_000_000:
                return f"R$ {num / 1_000_000_000:,.1f} Bilhões".replace('.', ',')
            elif num >= 1_000_000:
                return f"R$ {num / 1_000_000:,.1f} Milhões".replace('.', ',')
            elif num >= 1_000:
                return f"R$ {num / 1_000:,.1f} Mil".replace('.', ',')
            else:
                return f"R$ {num:,.2f}".replace('.', ',')
        except ValueError:
            pass
    if "R$" not in valor_str.upper() and any(c.isdigit() for c in valor_str):
        return f"R$ {valor_str}"
    return valor_str

def limpar_lead_score(score_raw):
    """Garante que o Lead Score retornado pela IA seja sempre um inteiro seguro (0-100)."""
    try:
        score_str = str(score_raw).replace('%', '').split('/')[0].strip()
        score_limpo = re.sub(r'[^0-9]', '', score_str)
        return int(score_limpo) if score_limpo else 50
    except Exception:
        return 50

def extrair_cnpj(texto):
    """Procura matematicamente pelo padrão exato de um CNPJ num texto sujo."""
    padrao_cnpj = r'\b\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}\b'
    cnpjs_encontrados = re.findall(padrao_cnpj, texto)
    # Retorna o primeiro CNPJ encontrado que não seja uma máscara vazia (como 00.000.000/0000-00)
    for cnpj in cnpjs_encontrados:
        if cnpj != "00.000.000/0000-00":
            return cnpj
    return "Não encontrado"

def gerar_template_excel():
    """Gera ficheiro Excel em memória para Onboarding do utilizador."""
    df_template = pd.DataFrame({"Empresas": ["Banco Pan", "Totvs", "Nubank"]})
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine='openpyxl') as writer:
        df_template.to_excel(writer, index=False, sheet_name='Template NextLead')
    return buffer

# =============================================================================
# 1. MOTORES DE PESQUISA (Web Scraping Robusto)
# =============================================================================
def buscar_ddg_seguro(query, max_res=2):
    try:
        resultados = DDGS().text(query, max_results=max_res)
        return " | ".join([res.get('body', '') for res in resultados]) if resultados else "Sem dados."
    except Exception:
        return "Sem dados."

def buscar_dados_reais(nome_empresa):
    site_oficial = "Não encontrado"
    linkedin_empresa = "Não encontrado"
    info_pessoas = "Não encontrado"
    cnpj_real = "Não encontrado"
    
    # Busca Domínio Clearbit
    try:
        url_clearbit = f"https://autocomplete.clearbit.com/v1/companies/suggest?query={nome_empresa}"
        resposta = requests.get(url_clearbit, headers={"User-Agent": "Mozilla/5.0"}, timeout=5)
        if resposta.status_code == 200 and resposta.json():
            site_oficial = "https://www." + resposta.json()[0].get("domain", "")
    except Exception:
        pass

    # Busca LinkedIn da Empresa
    try:
        busca_lkd = DDGS().text(f'{nome_empresa} linkedin company oficial brasil', max_results=2)
        if busca_lkd:
            for res in busca_lkd:
                link = res.get('href', '')
                if 'linkedin.com/company/' in link:
                    linkedin_empresa = link
                    break
    except Exception:
        pass

    # NOVO MOTOR: Busca e Extração de CNPJ com Regex
    try:
        busca_cnpj = DDGS().text(f'"{nome_empresa}" CNPJ matriz', max_results=3)
        if busca_cnpj:
            texto_sujo_cnpj = " ".join([res.get('body', '') for res in busca_cnpj])
            cnpj_real = extrair_cnpj(texto_sujo_cnpj)
    except Exception:
        pass

    # Busca de Executivos
    try:
        pessoas_encontradas = []
        busca_direta = DDGS().text(f'quem é o CEO ou diretor da empresa {nome_empresa}', max_results=2)
        if busca_direta:
            pessoas_encontradas.extend([res.get('body', '') for res in busca_direta])
            
        busca_noticias = DDGS().news(f'{nome_empresa} executivo', max_results=2)
        if busca_noticias:
            pessoas_encontradas.extend([f"{res.get('title', '')} - {res.get('body', '')}" for res in busca_noticias])
            
        if pessoas_encontradas:
            info_pessoas = " | ".join(pessoas_encontradas)
    except Exception:
        pass

    info_faturamento = buscar_ddg_seguro(f'"{nome_empresa}" faturamento receita anual milhoes bilhoes')
    info_funcionarios = buscar_ddg_seguro(f'"{nome_empresa}" quantidade numero de funcionarios empregados')
    info_noticias = buscar_ddg_seguro(f'"{nome_empresa}" notícias recentes investimento expansão inovação tecnologia')
        
    if linkedin_empresa == "Não encontrado":
        linkedin_empresa = f"https://www.linkedin.com/company/{formatar_nome_linkedin(nome_empresa)}/"

    return site_oficial, linkedin_empresa, cnpj_real, info_faturamento, info_funcionarios, info_noticias, info_pessoas

# =============================================================================
# 2. MOTORES DE INTELIGÊNCIA ARTIFICIAL
# =============================================================================
def descobrir_novos_leads_ia(api_key, proposta_valor, icp, quantidade):
    client = OpenAI(api_key=api_key)
    prompt = f"""
    Você é um especialista em inteligência de mercado no Brasil.
    A nossa empresa vende: {proposta_valor}
    O nosso Perfil de Cliente Ideal (ICP) é: {icp}
    
    Sua tarefa é agir como um radar de mercado e descobrir ATÉ {quantidade} EMPRESAS REAIS e conhecidas no mercado brasileiro que se encaixem perfeitamente nesse perfil.
    REGRA DE OURO: Retorne APENAS o número de empresas que você tem 100% de certeza que existem. NÃO invente nomes.
    
    Responda ESTRITAMENTE em formato JSON puro, com uma única chave "empresas" contendo uma lista de strings com os nomes oficiais dessas empresas.
    Não use marcações markdown como ```json.
    """
    try:
        response = client.chat.completions.create(model="gpt-4o-mini", messages=[{"role": "system", "content": prompt}], temperature=0.7)
        resposta_texto = response.choices[0].message.content.replace('```json', '').replace('```', '').strip()
        return json.loads(resposta_texto).get("empresas", [])
    except Exception:
        return []

def analisar_empresas_com_ia(lista_empresas, api_key, proposta_valor, nome_minha_empresa, site_minha_empresa):
    client = OpenAI(api_key=api_key)
    empresas_com_contexto = []
    
    barra_progresso = st.progress(0)
    texto_status = st.empty()
    texto_status.write(f"🔍 A vasculhar a web em paralelo para {len(lista_empresas)} empresas...")

    def buscar_e_formatar(empresa):
        # Agora a função de busca devolve também o cnpj_real (3º parâmetro)
        site, lkd, cnpj_real, fat, func, notic, pessoas = buscar_dados_reais(empresa)
        return f"Empresa: {empresa} | CNPJ Verificado: {cnpj_real} | Site: {site} | Lkd: {lkd} | Fat: {fat} | Func: {func} | Menções de Pessoas: {pessoas} | Notícias: {notic}"

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futuros = {executor.submit(buscar_e_formatar, emp): emp for emp in lista_empresas}
        for i, futuro in enumerate(concurrent.futures.as_completed(futuros)):
            empresas_com_contexto.append(futuro.result())
            barra_progresso.progress((i + 1) / len(lista_empresas))
            
    texto_status.write("🧠 A processar inteligência com a OpenAI...")
    contexto_formatado = "\n".join(empresas_com_contexto)
    
    contexto_vendedor = f"A nossa empresa vende estritamente: {proposta_valor}."
    if nome_minha_empresa.strip(): contexto_vendedor += f"\nO nome da nossa empresa é: {nome_minha_empresa.strip()}."
    if site_minha_empresa.strip(): contexto_vendedor += f"\nO nosso site oficial é: {site_minha_empresa.strip()}."
    
    prompt_sistema = f"""
    Você é um assistente comercial B2B tech.
    {contexto_vendedor}
    
    Gere um JSON com as seguintes chaves para cada empresa fornecida no contexto:
    1. "Empresa": Nome.
    2. "Site Oficial": Link.
    3. "LinkedIn da Empresa": Link.
    4. "CNPJ": Use ESTRITAMENTE o 'CNPJ Verificado' que lhe foi fornecido no contexto de cada empresa. Se disser 'Não encontrado', replique 'Não encontrado'. Não invente números.
    5. "Estado": Estado.
    6. "Município": Cidade.
    7. "Faixa de Faturamento": Estimativa em Reais.
    8. "Faixa de Funcionários": Estimativa.
    9. "Lead Score": Número 0-100.
    10. "Priorização": Alta, Média ou Baixa.
    11. "Justificativa": Motivo do score.
    12. "Concorrentes Diretos": 2 ou 3.
    13. "Gatilhos de Vendas": Baseado nas notícias.
    14. "Comite de Compras": 3 cargos.
    15. "Dores Mapeadas": 3 dores.
    16. "Organização do Funil": Etapa.
    17. "Cold Mail": Rascunho.
    18. "Mensagem LinkedIn": Rascunho curto.
    19. "Lookalikes": 3 empresas semelhantes.
    20. "Decisores Encontrados": Analise APENAS os textos em 'Menções de Pessoas'. Extraia os nomes reais e o cargo dessas pessoas. Devolva ESTRITAMENTE uma LISTA DE STRINGS simples (Ex: ["João Silva - CEO"]). Se não houver, devolva OBRIGATORIAMENTE uma lista vazia [].
    
    Responda ESTRITAMENTE em JSON puro, com a chave "analises" contendo a lista. Sem markdown.
    """
    
    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "system", "content": prompt_sistema}, {"role": "user", "content": f"Analise as seguintes empresas:\n{contexto_formatado}"}],
            temperature=0.2 
        )
        resposta_texto = response.choices[0].message.content.replace('```json', '').replace('```', '').strip()
        analises = json.loads(resposta_texto).get("analises", [])
        for item in analises:
            if "Faixa de Faturamento" in item: item["Faixa de Faturamento"] = formatar_faturamento(item["Faixa de Faturamento"])
            if "Lead Score" in item: item["Lead Score"] = limpar_lead_score(item["Lead Score"])
        texto_status.empty()
        return analises
    except Exception as e:
        st.error(f"Erro na comunicação ou conversão da IA: {e}")
        return None

# =============================================================================
# 3. INTERFACE DO UTILIZADOR (ONBOARDING & MAIN)
# =============================================================================
def main():
    st.set_page_config(page_title="NextLead | Inteligência B2B", page_icon="🎯", layout="centered", initial_sidebar_state="expanded")

    st.markdown("""
        <style>
        #MainMenu {visibility: hidden;} footer {visibility: hidden;}
        .stButton>button {width: 100%; border-radius: 8px; height: 50px; font-weight: bold;}
        </style>
    """, unsafe_allow_html=True)

    if "resultados_df" not in st.session_state: st.session_state.resultados_df = None
    if "api_key" not in st.session_state: st.session_state.api_key = ""
    if "ia_conectada" not in st.session_state: st.session_state.ia_conectada = False
    if "onboarding_completo" not in st.session_state: st.session_state.onboarding_completo = False
    if "nome_minha_empresa" not in st.session_state: st.session_state.nome_minha_empresa = ""
    if "site_minha_empresa" not in st.session_state: st.session_state.site_minha_empresa = ""
    if "proposta_valor" not in st.session_state: st.session_state.proposta_valor = ""
    if "icp" not in st.session_state: st.session_state.icp = ""

    if not st.session_state.onboarding_completo:
        col_img, col_txt = st.columns([1, 4])
        with col_img:
            st.image("https://cdn-icons-png.flaticon.com/512/3135/3135679.png", width=80)
        with col_txt:
            st.title("Bem-vindo ao NextLead 🚀")
            st.markdown("#### *Configure o seu motor de inteligência comercial*")
            
        st.write("Preencha as informações abaixo para calibrarmos a Inteligência Artificial para o seu negócio. Estes dados serão usados para analisar os leads, criar pontuações e redigir e-mails personalizados.")
        st.divider()

        with st.container():
            st.markdown("### 🏢 Os seus Dados")
            col1, col2 = st.columns(2)
            with col1:
                nome_input = st.text_input("Nome da sua Empresa (Opcional)", value=st.session_state.nome_minha_empresa, placeholder="Ex: Oracle Discovery")
            with col2:
                site_input = st.text_input("Site da sua Empresa (Opcional)", value=st.session_state.site_minha_empresa, placeholder="Ex: [www.oraclediscovery.com](https://www.oraclediscovery.com)")

            st.markdown("### 🎯 Estratégia B2B")
            proposta_input = st.text_area("O que você vende? (Obrigatório)*", value=st.session_state.proposta_valor, placeholder="Ex: Software de gestão financeira para médias empresas...", help="A IA vai cruzar o seu produto com as dores que encontrar na web.")
            icp_input = st.text_area("Perfil de Cliente Ideal - ICP (Obrigatório)*", value=st.session_state.icp, placeholder="Ex: Hospitais privados de grande porte no estado de São Paulo...", help="Defina quem você quer buscar para que a IA gere leads automáticos do zero.")

            st.markdown("### 🔑 Credenciais")
            api_input = st.text_input("Chave de Acesso (API Key da OpenAI)*", type="password", value=st.session_state.api_key, help="Cole aqui a sua chave sk-proj... para ligar o motor.")

            st.markdown("<br>", unsafe_allow_html=True)
            
            if st.button("Confirmar Perfil e Iniciar Plataforma", type="primary"):
                if not proposta_input.strip() or not icp_input.strip() or not api_input.strip():
                    st.error("⚠️ Atenção: Preencha os campos obrigatórios (O que você vende, ICP e Chave de Acesso) para continuar.")
                else:
                    st.session_state.nome_minha_empresa = nome_input
                    st.session_state.site_minha_empresa = site_input
                    st.session_state.proposta_valor = proposta_input
                    st.session_state.icp = icp_input
                    st.session_state.api_key = api_input
                    st.session_state.ia_conectada = True
                    st.session_state.onboarding_completo = True
                    st.rerun() 

    else:
        with st.sidebar:
            st.image("https://cdn-icons-png.flaticon.com/512/3135/3135679.png", width=60)
            st.title("NextLead")
            st.markdown("Motor de inteligência comercial.")
            st.divider()
            
            st.markdown("### 👤 O Seu Perfil")
            st.write(f"**Empresa:** {st.session_state.nome_minha_empresa or 'Não informado'}")
            
            with st.expander("Ver Estratégia de Vendas", expanded=False):
                st.markdown("**Produto/Oferta:**")
                st.caption(st.session_state.proposta_valor)
                st.markdown("**Cliente Ideal (ICP):**")
                st.caption(st.session_state.icp)

            st.divider()
            
            if st.session_state.ia_conectada:
                st.success("✅ Motor de IA Conectado")
            else:
                st.error("❌ Motor de IA Desconectado")
                
            if st.button("✏️ Editar Configurações"):
                st.session_state.onboarding_completo = False
                st.rerun()
                
            st.divider()
            st.caption("Versão 10.1.0 - CNPJ Regex Sourcing")

        st.title("NextLead 🚀")
        st.markdown("#### *Inteligência que transforma dados em negócios*")
        st.markdown("Acelere o seu ciclo de vendas com perfis detalhados, geração de leads e extração inteligente de decisores.")
        st.divider()

        aba_texto, aba_planilha, aba_descoberta = st.tabs(["✍️ 1 Empresa", "📊 Planilha (Lote)", "🔍 Descobrir Novos Leads"])
        empresas_para_analisar = []
        iniciar_analise = False
        modo_descoberta = False
        qtd_pedida = 0

        proposta_valor = st.session_state.proposta_valor
        icp = st.session_state.icp
        nome_minha_empresa = st.session_state.nome_minha_empresa
        site_minha_empresa = st.session_state.site_minha_empresa
        API_KEY = st.session_state.api_key

        with aba_texto:
            st.markdown("<br>", unsafe_allow_html=True)
            nome_empresa = st.text_input("Que empresa deseja prospectar hoje?")
            _, col2, _ = st.columns([1, 2, 1])
            with col2:
                if st.button("Gerar Inteligência de Vendas", type="primary", key="btn_unica"):
                    if nome_empresa:
                        empresas_para_analisar = [nome_empresa]
                        iniciar_analise = True
                    else: st.warning("⚠️ O nome da empresa não pode ficar vazio.")

        with aba_planilha:
            st.markdown("<br>", unsafe_allow_html=True)
            st.download_button(label="📄 Descarregar Ficheiro de Exemplo", data=gerar_template_excel().getvalue(), file_name="template_nextlead.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            arquivo_upload = st.file_uploader("Arraste o seu ficheiro Excel para aqui (.xlsx)", type=["xlsx"])
            if st.button("Processar Lote de Empresas", type="primary", key="btn_lote"):
                if arquivo_upload is not None:
                    try:
                        df_entrada = pd.read_excel(arquivo_upload)
                        if not df_entrada.empty:
                            empresas_para_analisar = df_entrada.iloc[:, 0].dropna().astype(str).tolist()
                            iniciar_analise = True
                    except: st.error("Erro ao ler Excel.")
                else: st.warning("⚠️ Nenhum ficheiro inserido.")

        with aba_descoberta:
            st.markdown("<br>", unsafe_allow_html=True)
            st.markdown("Deixe a nossa IA encontrar as empresas ideais para você baseada no seu Perfil de Cliente Ideal.")
            qtd_leads = st.slider("Quantos leads automáticos deseja gerar?", min_value=1, max_value=20, value=5)
            _, col3, _ = st.columns([1, 2, 1])
            with col3:
                if st.button("Descobrir e Analisar Leads", type="primary", key="btn_descobrir"):
                    modo_descoberta = True
                    iniciar_analise = True
                    qtd_pedida = qtd_leads

        if iniciar_analise:
            if not st.session_state.ia_conectada or not API_KEY: 
                st.error("⚠️ Insira e confirme a Chave de Acesso nas Configurações.")
            else:
                with st.status("🚀 A iniciar o motor de inteligência NextLead...", expanded=True) as status:
                    if modo_descoberta:
                        st.write(f"🕵️‍♀️ A mapear mercado para {qtd_pedida} empresas '{icp}'...")
                        leads_encontrados = descobrir_novos_leads_ia(API_KEY, proposta_valor, icp, qtd_pedida)
                        if not leads_encontrados:
                            status.update(label="Falha ao descobrir novos leads.", state="error")
                            st.stop()
                        qtd_encontrada = len(leads_encontrados)
                        if qtd_encontrada < qtd_pedida:
                            st.warning(f"⚠️ Apenas **{qtd_encontrada} leads** reais foram encontrados.")
                            st.info("💡 A IA foca em empresas reais. Se o nicho for muito estreito, ela recusa-se a inventar nomes.")
                        empresas_para_analisar = leads_encontrados
                    
                    resultados = analisar_empresas_com_ia(empresas_para_analisar, API_KEY, proposta_valor, nome_minha_empresa, site_minha_empresa)
                    if resultados:
                        status.update(label="Análise concluída com sucesso!", state="complete", expanded=False)
                        df_res_temp = pd.DataFrame(resultados)
                        df_res_temp['Lead Score'] = pd.to_numeric(df_res_temp['Lead Score'], errors='coerce').fillna(50).astype(int)
                        df_res_temp = df_res_temp.sort_values(by='Lead Score', ascending=False).reset_index(drop=True)
                        st.session_state.resultados_df = df_res_temp
                    else: status.update(label="Falha no processamento.", state="error")

        if st.session_state.resultados_df is not None:
            df_res = st.session_state.resultados_df
            st.markdown("<br><hr>", unsafe_allow_html=True)
            
            st.markdown("### 📊 Dashboard Gerencial")
            total_leads = len(df_res)
            df_res['Priorização_Limpa'] = df_res['Priorização'].astype(str).str.upper()
            prioridade_counts = df_res['Priorização_Limpa'].value_counts()
            alta = prioridade_counts.get('ALTA', 0)
            media_score = int(df_res['Lead Score'].mean())
            aproveitamento = int((alta / total_leads) * 100) if total_leads > 0 else 0

            col_m1, col_m2, col_m3, col_m4 = st.columns(4)
            col_m1.metric("Total Analisado", total_leads)
            col_m2.metric("Prioridade Alta 🔥", int(alta))
            col_m3.metric("Score Médio 🎯", f"{media_score}/100")
            col_m4.metric("Aproveitamento", f"{aproveitamento}%")
            st.markdown("<br>", unsafe_allow_html=True)
            
            col_g1, col_g2 = st.columns(2)
            with col_g1: st.markdown("**Distribuição**"); st.bar_chart(prioridade_counts, color="#ff4b4b")
            with col_g2: st.markdown("**Ranking de Score**"); st.bar_chart(df_res[['Empresa', 'Lead Score']].set_index('Empresa'), color="#0068c9")
                
            st.markdown("<br>", unsafe_allow_html=True)
            st.markdown("### 🏆 Painel de Oportunidades")
            
            for index, row in df_res.iterrows():
                prioridade = str(row.get('Priorização', 'Média')).upper()
                icone = "🔴" if "ALTA" in prioridade else "🟡" if "MÉDIA" in prioridade else "🔵"
                
                with st.expander(f"{icone} {row.get('Empresa', 'Empresa')} — Prioridade: {prioridade} | Score: {row.get('Lead Score', 50)}", expanded=False):
                    score_num = int(row.get('Lead Score', 50))
                    cor_score = "green" if score_num >= 80 else ("orange" if score_num >= 50 else "red")
                    st.markdown(f"**📈 Lead Score:** :{cor_score}[**{score_num}/100**]")
                    st.progress(score_num / 100.0)
                    st.markdown("<br>", unsafe_allow_html=True)

                    st.markdown("#### 🏢 Dados da Conta")
                    col_a, col_b = st.columns(2)
                    with col_a:
                        st.markdown(f"**🌐 Site:** [{row.get('Site Oficial', 'Link')}]({row.get('Site Oficial', '#')})")
                        st.markdown(f"**📍 Local:** {row.get('Município', '-')} / {row.get('Estado', '-')}")
                        st.markdown(f"**💰 Faturamento:** {row.get('Faixa de Faturamento', 'N/A')}")
                    with col_b:
                        st.markdown(f"**💼 LinkedIn:** [Acessar Perfil]({row.get('LinkedIn da Empresa', '#')})")
                        # O CNPJ agora é alimentado diretamente pela regex do Python
                        st.markdown(f"**📄 CNPJ:** {row.get('CNPJ', '-')}")
                        st.markdown(f"**👥 Funcionários:** {row.get('Faixa de Funcionários', 'N/A')}")
                        
                    st.divider()
                    st.markdown("#### 🧠 Inteligência Estratégica")
                    st.markdown(f"**⚔️ Concorrentes Diretos:** {row.get('Concorrentes Diretos', 'N/A')}")
                    st.markdown(f"**🔥 Gatilho de Vendas:**\n{row.get('Gatilhos de Vendas', 'Nenhum detetado.')}")
                    st.markdown(f"**🎯 Comité de Compras Sugerido:** {row.get('Comite de Compras', 'N/A')}")
                    st.markdown(f"**⚠️ Principais Dores:**\n{row.get('Dores Mapeadas', 'N/A')}")
                    st.markdown(f"**📊 Funil:** {row.get('Organização do Funil', 'N/A')} | **Justificativa:** {row.get('Justificativa', 'N/A')}")
                    
                    st.divider()
                    
                    st.markdown("#### 👥 Tomadores de Decisão (Pesquisa no LinkedIn)")
                    
                    decisores_ia = row.get('Decisores Encontrados', [])
                    empresa_nome = str(row.get('Empresa', 'Empresa')).replace(' ', '+')
                    
                    if not isinstance(decisores_ia, list):
                        decisores_ia = []
                    
                    if len(decisores_ia) > 0:
                        st.success("✅ Nomes reais detetados em notícias recentes:")
                        for nome_cargo in decisores_ia:
                            nome_limpo = str(nome_cargo).replace('"', '').replace('[', '').replace(']', '')
                            link_busca = f"https://www.linkedin.com/search/results/people/?keywords={nome_limpo.replace(' ', '+')}+{empresa_nome}"
                            st.markdown(f"- {nome_limpo} — [🔍 Buscar Perfil Exato]({link_busca})")
                    else:
                        st.warning("⚠️ Nomes não constam nas notícias recentes. Atalhos de pesquisa por cargo gerados:")
                        cargos_estrategicos = ["CEO", "Diretor", "Gerente"]
                        for cargo in cargos_estrategicos:
                            link_busca = f"https://www.linkedin.com/search/results/people/?keywords={cargo}+{empresa_nome}"
                            st.markdown(f"- {cargo} da empresa — [🔍 Buscar {cargo} no LinkedIn]({link_busca})")
                    
                    st.divider()
                    
                    st.markdown("#### 🚀 Expansão de Pipeline (Lookalikes)")
                    st.info(f"**Empresas semelhantes:**\n\n{row.get('Lookalikes', 'N/A')}")
                    
                    st.divider()

                    st.markdown("#### ✉️ Abordagem Pronta a Usar")
                    col_mail, col_lkd = st.columns(2)
                    with col_mail:
                        st.markdown("**Rascunho de E-mail:**")
                        st.info(row.get('Cold Mail', 'Texto não gerado.'))
                    with col_lkd:
                        st.markdown("**Convite LinkedIn:**")
                        st.success(row.get('Mensagem LinkedIn', 'Texto não gerado.'))
            
            st.markdown("<br>", unsafe_allow_html=True)
            
            df_export = df_res.drop(columns=['Priorização_Limpa'], errors='ignore')
            for col in df_export.columns:
                df_export[col] = df_export[col].apply(lambda x: ', '.join(x) if isinstance(x, list) else x)
                
            buffer = io.BytesIO()
            with pd.ExcelWriter(buffer, engine='openpyxl') as writer: df_export.to_excel(writer, index=False, sheet_name='Leads_Enriquecidos')
            
            _, col_dl, _ = st.columns([1, 2, 1])
            with col_dl: st.download_button(label="📥 Exportar Base Completa para CRM", data=buffer.getvalue(), file_name="nextlead_insights.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", type="primary")

if __name__ == "__main__":
    main()