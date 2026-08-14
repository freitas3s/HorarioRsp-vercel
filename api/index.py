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

    b64_creds = os.environ.get("GOOGLE_CREDENTIALS_BASE64")
    if not b64_creds:
        raise HTTPException(
            status_code=500, 
            detail="A variável GOOGLE_CREDENTIALS_BASE64 não foi encontrada na Vercel."
        )

    try:
        # Decodifica a string Base64 de volta para o JSON original
        json_bytes = base64.b64decode(b64_creds)
        service_account_info = json.loads(json_bytes.decode("utf-8"))
        
        creds = Credentials.from_service_account_info(service_account_info, scopes=scope)
        return gspread.authorize(creds)
    except Exception as e:
        raise HTTPException(
            status_code=500, 
            detail=f"Erro ao decodificar credenciais Base64: {str(e)}"
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
                msg = f"✅ Cadastro realizado com sucesso!\nOperador: *{operador}*\nVocê receberá notificações aqui sempre que sua escala mudar."
            else:
                msg = "Para se cadastrar, envie sua identificação da escala assim:\n`/start 3S REBITTE`"
            
            requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={
                "chat_id": chat_id, "text": msg, "parse_mode": "Markdown"
            })
    return {"status": "ok"}


@app.post("/api/webhook-planilha")
def webhook_planilha(data: dict = Body(...)):
    """Recebe avisos do Google Sheets quando a planilha for editada e notifica o Telegram."""
    operador = data.get("operador", "").upper().strip()
    alteracao = data.get("alteracao", "")

    chat_id = buscar_chat_id(operador)
    if not chat_id:
        return {"status": "ignorado", "motivo": f"Chat ID não cadastrado para {operador}"}

    mensagem = (
        f"⚠️ *Alteração na Escala RSP*\n\n"
        f"Olá, *{operador}*!\n"
        f"Sua escala foi alterada na planilha:\n\n"
        f"📌 {alteracao}"
    )

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    res = requests.post(url, json={"chat_id": chat_id, "text": mensagem, "parse_mode": "Markdown"})

    return {"status": "sucesso" if res.status_code == 200 else "erro"}