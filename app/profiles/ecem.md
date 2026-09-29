# Ecem Yalçın — fact sheet for the ecem.dev voice assistant

Source: https://ecem.dev (portfolio, 2026-09-29). The assistant may ONLY use the facts
below. Anything not listed here is unknown.

## Summary
- Ecem Yalçın — AI Engineer focused on speech, vision and language. Based in İstanbul (Beşiktaş).
- Open to AI/ML engineering roles and collaborations.
- Currently AI Engineer at CallMetric AI, building emotion AI: in-vehicle emotion recognition,
  emotion analysis on call-center conversations, real-time live coaching for agents.
- Computer Engineering graduate (B.Sc.) from Kocaeli University (09/2022 – 06/2026).
- Organizer at Google Developer Groups (GDG) Kocaeli since 2024 — AI, cloud and software events.
- Languages: Turkish (native), English (professional working proficiency).

## Career
- CallMetric AI — AI Engineer, 08/2026 – present, Düzce, Türkiye.
  - In-vehicle speech emotion recognition as continuous valence–arousal prediction; benchmarked
    Whisper, Wav2Vec2 and emotion-specific encoders; noise-augmented training; English/French
    cross-lingual evaluation.
  - Transcribes call-center audio and runs emotion & sentiment analysis on speech and transcripts.
  - Building a live coaching system that guides call-center agents in real time based on the
    emotional flow of the conversation.
  - Built an end-to-end speech emotion recognition pipeline (data sourcing → preprocessing →
    training), predicting valence–arousal and emotion categories.
  - Integrated models with a fine-tuned Turkish Whisper ASR and speaker diarization on production
    call data. Worked on AWS VMs over SSH, RabbitMQ request flows and CRM integration.
- DecodeLabs — Data Science Intern, 03/2026 – 06/2026, remote (India). Data cleaning, EDA,
  statistics, feature engineering with Python, Pandas, NumPy, SQL.
- Erkba (Bilişim Vadisi) — Data & AI Intern, 06/2025 – 11/2025, Gebze. YOLOv8 + ByteTrack fish
  detection and tracking (93.5% mAP@50, 91.3% precision, 88.1% recall); image processing pipelines
  for computer vision and industrial inspection (PyTorch, OpenCV, PCA, K-Means, Otsu).
- Kocaeli University — AI Researcher, 06/2024 – 05/2026. Skin cancer detection and medical image
  analysis; augmentation and CNN-based classification.
- Toyota — Backend / Full Stack Developer Intern, 07/2024 – 08/2024, Sakarya. ASP.NET, C#, MSSQL,
  RESTful APIs, SQL queries and schemas.

## Featured projects
- In-Vehicle Speech Emotion Recognition (CallMetric AI case study): continuous valence–arousal,
  robustness to cabin noise; encoder benchmarking, noise augmentation, cross-lingual evaluation,
  model selection with CCC, freeze-depth ablations, latency/footprint benchmarking. Lesson: "good on
  clean data" and "production-reliable" are different claims; report honest negative results.
  Confidential: source code is not public.
- Call-Center Emotion Analytics & Live Coaching (CallMetric AI case study): live call audio →
  fine-tuned Turkish ASR → diarization → emotion trajectory → live coaching for agents during the
  call. Streaming inference, RabbitMQ, AWS. Confidential: source code is not public.
- Music Emotion Recognition & Emotion-to-Art Engine (bachelor's thesis, 2026): valence–arousal
  from music (DEAM, 1,802 tracks); CNN, CRNN (CNN+BiLSTM), Audio Transformer and hybrids; 24+
  experiments; weighted ensemble RMSE 0.8136 (best single model 0.8311); Pearson 0.73 valence /
  0.79 arousal. Survey with 298 participants: human–model distance 0.91 vs human–Gemini 1.32
  (Gemini 2.5 Flash captured mood but misread arousal). Emotion-to-Art engine turns emotion
  profiles into prompts for FLUX.1-schnell. Transformers underperformed CNNs (dataset too small);
  ensembles beat every single model. Team project with Beril İsalar; advisor Dr. Hikmetcan Özcan;
  exhibited June 2026.
- Enterprise AI Document Intelligence: ASP.NET Core, Clean/Layered Architecture, RAG, PostgreSQL,
  Qdrant, RabbitMQ, async processing.
- Fintech Credit Risk Engine: real-time credit risk with Kafka → Spark → Delta Lake → MLflow,
  Spark MLlib models (Lending Club dataset).
- AI Research Agent: RAG-based multi-agent assistant (planner → researcher → writer) with
  LangGraph and FastAPI for literature research and report generation.
- Gemini Document Explainer API: document Q&A with chunking, embeddings, cosine-similarity retrieval.
- LiteVision Studio: in-browser computer-vision playground (object, hand, face, pose, OCR) running
  fully client-side with LiteRT.js, WebGPU, MediaPipe, TypeScript; FPS and latency benchmarking.
- Fish Detection & Tracking: YOLOv8 + ByteTrack, 93.5% mAP@50.
- Skin Cancer Classification: CNN, PyTorch, augmentation.
- More on GitHub (github.com/ecemy3): bird species detection, Parkinson's detection from voice
  (scikit-learn, XGBoost), fake news detection (Streamlit), mini RAG with Gemini, birth rate
  analysis (Prophet, ARIMA), Veri Avcısı (search in Excel/PDF files), and others.
- This voice assistant itself: real-time multimodal voice assistant built with Google ADK, Gemini
  Live (gemini-3.8-live) on Vertex AI, FastAPI and WebSockets — supports barge-in, tool calls,
  camera/screen sharing, Gemini Flash + Google Search research, and a call-center Agent Assist mode.

## Skills
- AI/ML: PyTorch, scikit-learn, CNN, CRNN, Transformers, YOLOv8, OpenCV, LLMs, RAG, Whisper,
  audio processing, speech emotion recognition.
- Data: Kafka, Spark, Delta Lake, MLflow, Pandas, NumPy, SQL, EDA, feature engineering.
- Backend: FastAPI, Python, ASP.NET Core, C#, PostgreSQL, MSSQL, RabbitMQ, Redis, Clean Architecture.
- Infra & agents: AWS, SSH, Docker, LangGraph, Qdrant, Gemini, FLUX.1, Git, GitHub.

## Speaking & community
- Build with AI Türkiye, May 2026 — speaker & instructor: "Designing Smart Decision Engines with
  Vibe Coding" (hands-on training for Google Developer Communities Turkey).
- GDG on Campus Düzce University, 2026 — "GDG in Woman — Women in Tech": data science & AI,
  her journey as a GDG Kocaeli organizer.

## Interests
Computer vision, speech AI, generative AI, LLMs, data engineering, multi-agent systems, MLOps,
manufacturing automation.

## Contact (public on the site)
- Email: ecemyalcin3434@gmail.com
- LinkedIn: linkedin.com/in/ecem-yalcin
- GitHub: github.com/ecemy3
- Community: @gdgkocaeli
- To book a meeting, visitors can use the "Book a meeting" button on the site.
