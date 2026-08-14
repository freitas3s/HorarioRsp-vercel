import os
import json
from datetime import datetime, timedelta
import pandas as pd
import gspread
from google.oauth2.service_account import Credentials
from fastapi import FastAPI, HTTPException, Query, Request, Body
import requests
import base64

app = FastAPI()

# ID da sua planilha Google Sheets
SPREADSHEET_ID = "1Li5g5tWWL8VbxrVbhXTFNBu3aLzM8_ETXXixTMVgA_8"

# Token do seu Bot no Telegram
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "8124239925:AAGiWLWqn8oPjEzji-5k9x7GXOxQ5DRQ39A")

def conectar_google_sheets():
    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive"
    ]

    raw_env = os.getenv("GOOGLE_CREDENTIALS_JSON") or os.getenv("GOOGLE_CREDENTIALS_BASE64")
    
    if not raw_env or raw_env == "{}":
        raise HTTPException(
            status_code=500, 
            detail="A variável de credenciais do Google não foi encontrada na Vercel."
        )

    service_account_info = None

    # TENTATIVA 1: Decodificar Base64
    try:
        json_bytes = base64.b64decode(raw_env)
        service_account_info = json.loads(json_bytes.decode("utf-8"))
    except Exception:
        service_account_info = None

    # TENTATIVA 2: Decodificar JSON direto
    if not service_account_info:
        try:
            json_limpo = raw_env.replace("\\n", "\n")
            service_account_info = json.loads(json_limpo, strict=False)
        except Exception as e:
            raise HTTPException(
                status_code=500,
                detail=f"Falha ao ler o formato da chave: {str(e)}"
            )

    # CORREÇÃO CRÍTICA: Trata as quebras de linha da chave privada em ambos os casos
    if service_account_info and "private_key" in service_account_info:
        pk = service_account_info["private_key"]
        # Converte qualquer \\n literal em quebra de linha real \n
        service_account_info["private_key"] = pk.replace("\\n", "\n")

    try:
        creds = Credentials.from_service_account_info(service_account_info, scopes=scope)
        return gspread.authorize(creds)
    except Exception as e:
        raise HTTPException(
            status_code=500, 
            detail=f"Erro de autenticação no Google: {str(e)}"
        )

    # --- CADASTRO AUTOMÁTICO DE USUÁRIOS DO TELEGRAM ---
def get_ou_criar_aba_cadastros(client):
    sh = client.open_by_key(SPREADSHEET_ID)
    try:
        return sh.worksheet("TELEGRAM_CADASTROS")
    except:
        aba = sh.add_worksheet(title="TELEGRAM_CADASTROS", rows="100", cols="2")
        aba.append_row(["OPERADOR", "CHAT_ID"])
        return aba

def salvar_cadastro_telegram(operador, chat_id):
    try:
        aba = get_ou_criar_aba_cadastros(conectar_google_sheets())
        registros = aba.get_all_records()
        
        for idx, row in enumerate(registros, start=2):
            if str(row.get("OPERADOR")).upper().strip() == operador:
                aba.update_cell(idx, 2, str(chat_id))
                return
                
        aba.append_row([operador, str(chat_id)])
    except Exception as e:
        print(f"Erro ao salvar cadastro: {e}")

def buscar_chat_id(operador):
    try:
        aba = get_ou_criar_aba_cadastros(conectar_google_sheets())
        registros = aba.get_all_records()
        for row in registros:
            if str(row.get("OPERADOR")).upper().strip() == operador.upper().strip():
                return str(row.get("CHAT_ID"))
    except Exception as e:
        print(f"Erro ao buscar chat_id: {e}")
    return None


# --- AUXILIARES E LÓGICA DA ESCALA ---

# --- FUNÇÃO AUXILIAR DE FILTRO TEMPORAL ---
def tempo_em_minutos(hora_str, turno_nome):
    """Converte o horário HH:MM em minutos. Trata a virada de dia no turno PERNOITE."""
    h, m = map(int, hora_str.split(":"))
    minutos = h * 60 + m
    # No turno PERNOITE, horários de madrugada (ex: 01:00) ganham +24h em minutos
    if turno_nome == "PERNOITE" and h < 12:
        minutos += 1440
    return minutos

def gerar_horarios_base(turno):
    if turno == "MANHÃ":
        inicio, fim = "06:45", "14:00"
    elif turno == "TARDE":
        inicio, fim = "14:15", "23:00"
    else:
        inicio, fim = "23:15", "06:30"

    lista = []
    atual = datetime.strptime(inicio, "%H:%M")
    final = datetime.strptime(fim, "%H:%M")

    if final < atual:
        final += timedelta(days=1)

    while atual <= final:
        lista.append(atual.strftime("%H:%M"))
        atual += timedelta(minutes=15)

    return lista

def buscar_escala_atual():
    client = conectar_google_sheets()
    sh = client.open_by_key(SPREADSHEET_ID)

    hora_h = (datetime.utcnow() - timedelta(hours=3)).hour
    prefixos_validos = ("1S", "2S", "3S", "SO")

    if 6 <= hora_h < 14:
        aba, intervalo, idx_nome = "MANHÃ", "C:AJ", 4
    elif 14 <= hora_h < 23:
        aba, intervalo, idx_nome = "TARDE", "H:AW", 6
    else:
        aba, intervalo, idx_nome = "PERNOITE", "C:AH", 2

    sheet = sh.worksheet(aba)
    valores = sheet.get(intervalo)

    if not valores:
        return None, aba

    horas = gerar_horarios_base(aba)
    escala_filtrada = [{'linha_completa': horas}]

    for linha in valores:
        p1 = str(linha[idx_nome]).strip().upper() if len(linha) > idx_nome else ""
        p2 = str(linha[0]).strip().upper() if len(linha) > 0 else ""

        nome_final = p2 if p2.startswith(prefixos_validos) else (p1 if p1.startswith(prefixos_validos) else "")

        if nome_final:
            escala_filtrada.append({
                "nome": nome_final,
                "linha_completa": linha[idx_nome:]
            })

    return pd.DataFrame(escala_filtrada), aba

def analisar_rendicoes_v2(df, nome_busca, turno_nome):
    if df is None or df.empty:
        return []

    horarios_base = gerar_horarios_base(turno_nome)
    mask = df['nome'].str.contains(nome_busca.upper(), na=False)
    
    if not mask.any():
        return []

    linha_usuario = df[mask].iloc[0]['linha_completa']
    eventos = []
    letras_ocupado = ["X", "C", "F"]

    for i in range(len(horarios_base)):
        status_atual = str(linha_usuario[i]).strip().upper() if i < len(linha_usuario) else ""
        status_anterior = str(linha_usuario[i-1]).strip().upper() if i > 0 and i-1 < len(linha_usuario) else ""

        # Entrada
        if status_atual in letras_ocupado and status_anterior not in letras_ocupado:
            saindo_agora = []
            for _, row in df.iterrows():
                if 'nome' not in row or row['nome'] == nome_busca: continue
                outra_linha = row['linha_completa']

                if i > 0 and i < len(outra_linha):
                    s_ant = str(outra_linha[i-1]).strip().upper()
                    s_atu = str(outra_linha[i]).strip().upper()

                    if s_ant in letras_ocupado and s_atu not in letras_ocupado and s_ant == status_atual:
                        tag = f"({s_ant})" if s_ant != "X" else ""
                        saindo_agora.append(f"{row['nome']} {tag}".strip())

            mapa = {"X": "Operador", "C": "Coordenador", "F": "Fis"}
            funcao = mapa.get(status_atual, "Operador")
            detalhe = f"Rendendo {', '.join(saindo_agora)}" if saindo_agora else f"Desagrupando como {funcao}"

            eventos.append({
                "hora": horarios_base[i],
                "funcao": funcao,
                "msg": f"🟢 Você entra como {funcao}",
                "detalhe": detalhe,
                "tipo": "entrada"
            })

        # Saída
        elif status_atual not in letras_ocupado and status_anterior in letras_ocupado:
            entrando_agora = []
            for _, row in df.iterrows():
                if 'nome' not in row or row['nome'] == nome_busca: continue
                outra_linha = row['linha_completa']

                if i < len(outra_linha):
                    s_ant = str(outra_linha[i-1]).strip().upper() if i > 0 else ""
                    s_atu = str(outra_linha[i]).strip().upper()

                    if s_ant not in letras_ocupado and s_atu in letras_ocupado and s_atu == status_anterior:
                        tag = f"({s_atu})" if s_atu != "X" else ""
                        entrando_agora.append(f"{row['nome']} {tag}".strip())

            detalhe = f"Sendo rendido por {', '.join(entrando_agora)}" if entrando_agora else "Agrupamento / Fim de turno"

            eventos.append({
                "hora": horarios_base[i],
                "funcao": "Saída",
                "msg": "🔴 Você sai do posto",
                "detalhe": detalhe,
                "tipo": "saida"
            })

    return eventos


# --- ENDPOINTS REST ---

@app.get("/api/escala")
def consultar_escala(nome: str = Query(...)):
    try:
        df_escala, turno_nome = buscar_escala_atual()
        if df_escala is None:
            raise HTTPException(status_code=404, detail="Escala não encontrada.")
        
        rendicoes = analisar_rendicoes_v2(df_escala, nome, turno_nome)
        return {"usuario": nome, "turno": turno_nome, "rendicoes": rendicoes}
    except Exception as e:
        # Retorna o nome da exceção e a mensagem real do erro
        mensagem_erro = f"{type(e).__name__}: {str(e)}"
        print(f"ERRO BACKEND: {mensagem_erro}")
        raise HTTPException(status_code=500, detail=mensagem_erro)

@app.post("/api/telegram-webhook")
async def telegram_webhook(request: Request):
    """Recebe mensagens do Telegram e registra o Chat ID do operador automaticamente."""
    data = await request.json()
    if "message" in data and "text" in data["message"]:
        chat_id = data["message"]["chat"]["id"]
        texto = data["message"]["text"].strip()

        if texto.startswith("/start"):
            partes = texto.split(" ", 1)
            if len(partes) > 1:
                operador = partes[1].upper().strip()
                salvar_cadastro_telegram(operador, chat_id)
                msg = f"✅ Cadastro realizado com sucesso!\nOperador: *{operador}*\nVocê receberá notificações aqui sempre que seu horario mudar."
            else:
                msg = "Para se cadastrar, envie sua identificação do horario assim:\n`/start 3S REBITTE`"
            
            requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={
                "chat_id": chat_id, "text": msg, "parse_mode": "Markdown"
            })
    return {"status": "ok"}


# --- ENDPOINT DO WEBHOOK ---
@app.post("/api/webhook-planilha")
def webhook_planilha(data: dict = Body(...)):
    """Recebe avisos do Google Sheets e notifica o operador APENAS sobre os próximos horários."""
    operador = data.get("operador", "").upper().strip()

    chat_id = buscar_chat_id(operador)
    if not chat_id:
        return {"status": "ignorado", "motivo": f"Chat ID não cadastrado para {operador}"}

    try:
        df_escala, turno_nome = buscar_escala_atual()
        
        if df_escala is None:
            resumo_rendicoes = "Não foi possível carregar seu horário."
        else:
            # 1. Busca todas as rendições calculadas na planilha
            todas_rendicoes = analisar_rendicoes_v2(df_escala, operador, turno_nome)
            
            # 2. Obtém a hora atual no horário de Brasília (UTC-3)
            agora = datetime.utcnow() - timedelta(hours=3)
            hora_atual_str = agora.strftime("%H:%M")
            minutos_agora = tempo_em_minutos(hora_atual_str, turno_nome)

            # 3. FILTRO: Mantém apenas os horários iguais ou posteriores ao horário atual
            rendicoes_futuras = [
                r for r in todas_rendicoes 
                if tempo_em_minutos(r["hora"], turno_nome) >= minutos_agora
            ]

            if not rendicoes_futuras:
                resumo_rendicoes = "Você não possui mais horários de rendição pendentes para o restante deste turno."
            else:
                linhas = []
                for r in rendicoes_futuras:
                    linhas.append(f"⏰ *{r['hora']}* — {r['msg']}\n   ↳ _{r['detalhe']}_")
                resumo_rendicoes = "\n\n".join(linhas)

        # Monta a mensagem final
        mensagem = (
            f"⚠️ *Seu horário foi atualizado!*\n\n"
            f"Olá, *{operador}*!\n"
            f"Turno: *{turno_nome}*\n\n"
            f"📋 *Seus próximos horários de rendição:*\n\n"
            f"{resumo_rendicoes}"
        )

        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        res = requests.post(url, json={"chat_id": chat_id, "text": mensagem, "parse_mode": "Markdown"})

        return {"status": "sucesso" if res.status_code == 200 else "erro"}

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))