@echo off
setlocal
set SVC=ViberMatrixBridge
set NSSM=%~dp0nssm.exe

net stop %SVC% 2>nul
"%NSSM%" remove %SVC% confirm
echo Done.
