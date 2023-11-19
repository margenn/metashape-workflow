#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# :autoIndent=simple:collapseFolds=0:indentSize=4:mode=python:noTabs=true:tabSize=4:wrap=soft:

"""
Este script automatiza o workflow de criacao de nuvem densa e ortofoto

Para executar, abra a janela MENU SUPERIOR > VIEW > CONSOLE > Clicar no 3o botão (Run Script) > Escolher este script > OK

PASSO A PASSO:
    1. Importe as fotos (Workflow > Add Photos)
    2. Desabilite todas as fotos desnecessarias (pouso, decolagem, baixa qualidade)
    3. Execute este script. (~2seg) (Calculo da qualidade das fotos e remocao das desabilitadas)
    4. Execute este script (2a vez). (~10min) (Calculo da nuvem esparsa e alinhamento das fotos)
    6. Caso haja GCPs, Importe-os, faca os marks em cada foto, salve o projeto e mande realinhar:
        Reference > 1o botao > Importar txt > Ignorar GCPs que estiverem na beirada da foto
        Otimize as cameras e veja se o erro esta a niveis toleraveis. Salve o projeto
    7. Execute este script (3a vez). Inicio do processamento (~2horas). Ele vai:
        Calcular DepthMaps
        Calcular Dense Cloud, Classifica os ground points e remove pontos espurios (LowPoint)
        Calcular MESH (Opcional, pode ser desligado via: DesejaCalcularSurface)
        Calcular DEM (Altitude baseada nos DepthMaps. Necessario para calcular a ortofoto)
        Calcular DSM (Inclui vegetacao, predios. Nada e excluido)
        Calcular DEM somente com os ground points
        Calcular a ORTOFOTO
        Exportação:
            Nuvem de pontos em formato .las
            Ortofoto em .tif
            Nuvem de pontos em formato de aplicacao web .zip

REFERENCIAS:
https://github.com/dobedobedo/Metashape-Workflow/blob/master/Metashape_Workflow.py
"""


from pathlib import Path
from subprocess import check_output
import Metashape
import datetime
import os
import shutil
import sys
import traceback
import zipfile

doc = Metashape.app.document

################################################################################
# User variables. Faca os ajustes aqui antes de rodar o script
################################################################################

# Pasta de exportacao. Esta pasta Devera estar presente na pasta-pai da pasta do projeto, senao havera um erro
PastaDeExportacao = "saida" #Nome da pasta irma onde os itens serao exportados
PastaDeExportacaoCaminhoCompleto = "" #Deixe vazio para ser definido pelo programa
PotreeExe = "/home/ma/Documents/potree-converter/PotreeConverter" #Caminho pra aplicacao de criacao de paginas web

# VARIABLES FOR IMAGE QUALITY FILTER
QualityFilter = False #True, False
QualityCriteria = 0.7 #float number range from 0 to 1 (default 0.5)

# VARIABLES FOR PHOTO ALIGNMENT
# Accuracy = Metashape.Accuracy.HighAccuracy #HighestAccuracy, HighAccuracy, MediumAccuracy, LowAccuracy, LowestAccuracy
DownscaleAlignment = 1 # Highest=0 High=1 Medium=2 Low=4 Lowest=8
Key_Limit = 40000 #Numero inteiro, geralmente 40000 pontos
Tie_Limit = 4000 #Numero inteiro, geralmente 4000 e suficiente

# VARIAVEIS PARA CONSTRUIR OS DEPTHMAP (QUE SERA, POR CONSEQUENCIA, A RESOLUCAO DO DENSE CLOUD)
# Quality = Metashape.Quality.HighQuality #UltraQuality, HighQuality, MediumQuality, LowQuality, LowestQuality
DownscaleDepthMaps = 2 #Ultra=1 High=2 Medium=4 Low=8 Lowest=16 (Ultra = Mesmo GSD da foto)
FilterMode = Metashape.FilterMode.MildFiltering #AggressiveFiltering, ModerateFiltering, MildFiltering, NoFiltering
MaxNeighbors = 30 #Default=100. Reduzir este valor caso processamento demore muito

# VARIABLES FOR DENSE CLOUD GROUND POINT CLASSIFICATION.
DesejaClassificarGroundPoint = False #True, False
Max_Angle = 10 #Angulo maximo. Angulos maiores que estes, nao sera considerado "ground"
Cell_Size = 20 #Tamanho da Celula em metros. valores muito pequenos fazem telhados virarem "ground" tambem
Max_Distance = 1 #Maxima distancia em metros a ser avaliada na classificacao de solo. Aumentar se o calculo demorar

# VARIABLES FOR BUILDING ORTHOMOSAIC
BlendingMode = Metashape.BlendingMode.MosaicBlending #AverageBlending, MosaicBlending, MinBlending, MaxBlending, DisabledBlending
Color_correction = False #True, False (True depende de DesejaCalcularSurface=True)
Color_balance = False #True, False

# VARIABLES FOR BUILDING 3D MESH
DesejaCalcularSurface = False #True, False
Surface = Metashape.SurfaceType.HeightField #Arbitrary, HeightField
SurfaceSource = Metashape.DataSource.DepthMapsData #PointCloudData, DenseCloudData, DepthMapsData

# VARIAVEIS PARA DEM
DownscaleDem = 2  # 1 = mesma resolucao da nuvem de pontos (proces. demora MUITO) Geralmente 2 ou 4 é suficiente
DesejaCriarNovoDEMSomenteComGroundPoints = False #False, True
################################################################################



# INICIO DAS FUNCOES

def AlignPhoto(chunk, DownscaleAlignment, Key_Limit, Tie_Limit, QualityFilter, QualityCriteria):
    if QualityFilter:
        if chunk.cameras[0].meta['Image/Quality'] is None:
            chunk.analyzePhotos() # chunk.estimateImageQuality()
        for band in [band for camera in chunk.cameras for band in camera.planes]:
            if float(band.meta['Image/Quality']) < QualityCriteria:
                band.enabled = False
    chunk.matchPhotos(downscale=DownscaleAlignment,
                      generic_preselection=True,
                      reference_preselection=True,
                      filter_mask=False,
                      keypoint_limit=Key_Limit,
                      tiepoint_limit=Tie_Limit)
    chunk.alignCameras()
    chunk.optimizeCameras(fit_f=True, fit_cx=True, fit_cy=True, fit_b1=False, fit_b2=False,
                          fit_k1=True, fit_k2=True, fit_k3=True, fit_k4=False,
                          fit_p1=True, fit_p2=True, fit_p3=False, fit_p4=False,
                          adaptive_fitting=False, tiepoint_covariance=False)


def ConstruirDepthMaps(chunk, DownscaleDepthMaps, FilterMode, MaxNeighbors):
    print("DEBUG: DownscaleDepthMaps:%d, MaxNeighbors:%d, " % (DownscaleDepthMaps, MaxNeighbors))
    # TODO incluir limites de performance. max_workgroup_size workitem_size_cameras
    chunk.buildDepthMaps(downscale=DownscaleDepthMaps, filter_mode=FilterMode, reuse_depth=True, max_neighbors=MaxNeighbors)


def ConstruirNuvemDensa(chunk, MaxNeighbors):
    VerificarSeTodasAsFotosPossuemDepthMap(chunk)
    chunk.buildDenseCloud(point_colors=True, max_neighbors=MaxNeighbors)


def ClassificarPontosDeSolo(chunk, Max_Angle, Max_Distance, Cell_Size):
    # DEM_resolution, Image_resolution = GetResolution(chunk)
    chunk.dense_cloud.classifyGroundPoints(max_angle=Max_Angle, max_distance=Max_Distance, cell_size=Cell_Size)


def BuildModel(chunk):
    try:
        chunk.buildModel(surface=Surface,
                         interpolation=Metashape.Interpolation.EnabledInterpolation,
                         face_count=Metashape.FaceCount.HighFaceCount,
                         source_data=SurfaceSource,
                         vertex_colors=True)
    except:
        chunk.buildModel(surface=Surface,
                         interpolation=Metashape.Interpolation.EnabledInterpolation,
                         face_count=Metashape.FaceCount.HighFaceCount,
                         source_data=Metashape.DataSource.DenseCloudData,
                         vertex_colors=True)


def CalcularDSM(chunk, resolucao):
    # parte da funcao GetResolution
    try:
        chunk.buildDem(source_data=Metashape.DataSource.DepthMapsData,
                       interpolation=Metashape.Interpolation.EnabledInterpolation,
                       projection = chunk.crs,
                       resolution = resolucao)
    except:
        chunk.buildDem(source_data=Metashape.DataSource.DepthMapsData,
                       interpolation=Metashape.Interpolation.EnabledInterpolation,
                       resolution = resolucao)


def CalcularDEM(chunk, resolucao):
    try:
        chunk.buildDem(source_data=Metashape.DataSource.DepthMapsData,
                       interpolation=Metashape.Interpolation.EnabledInterpolation,
                       projection = chunk.crs,
                       classes=[Metashape.PointClass.Ground],
                       resolution = resolucao)
    except:
        chunk.buildDem(source_data=Metashape.DataSource.DepthMapsData,
                       interpolation=Metashape.Interpolation.EnabledInterpolation,
                       classes=[Metashape.PointClass.Ground],
                       resolution = resolucao)


def BuildMosaic(chunk, BlendingMode):
    try:
        chunk.buildOrthomosaic(surface_data=Metashape.DataSource.ElevationData,
                               blending_mode=BlendingMode,
                               color_correction=Color_correction,
                               fill_holes=True,
                               projection=chunk.crs)
    except:
        if Color_correction:
            chunk.calibrateColors(source_data=Metashape.DataSource.ModelData, color_balance=Color_balance)
        chunk.buildOrthomosaic(surface_data=Metashape.DataSource.ElevationData,
                               blending_mode=BlendingMode,
                               fill_holes=True)


def DefinirPastaDeExportacao():
    global PastaDeExportacaoCaminhoCompleto
    global PastaDeExportacao
    if PastaDeExportacaoCaminhoCompleto == "":
        project_path = Path(Metashape.app.document.path)
        if (project_path.parent / PastaDeExportacao).exists():
            PastaDeExportacaoCaminhoCompleto = str(project_path.parent / PastaDeExportacao) #parent
        elif (project_path.parent.parent / PastaDeExportacao).exists():
            PastaDeExportacaoCaminhoCompleto = str(project_path.parent.parent / PastaDeExportacao) #grandparent
        else:
            #PastaDeExportacao nao existe, entao cria uma
            PastaDeExportacaoCaminhoCompleto = str(project_path.parent.parent / PastaDeExportacao)
            Path(PastaDeExportacaoCaminhoCompleto).mkdir()
    if not Path(PastaDeExportacaoCaminhoCompleto).exists():
        raise RuntimeError('PASTA %s NAO EXISTE. EDITE ESTE SCRIPT E DEIXE ASSIM: PastaDeExportacaoCaminhoCompleto = ""' % PastaDeExportacaoCaminhoCompleto)
    printNovaAtividade("PASTA DE EXPORTACAO FOI DEFINIDA EM: %s" % PastaDeExportacaoCaminhoCompleto)

def printNovaAtividade(msg):
    msg = "\n################################################################################\n" + msg + "\n################################################################################"
    print(msg)




def StandardWorkflow(doc, chunk, **kwargs):
    if chunk.depth_maps is None:
        mensagemDepthMaps = "CALCULANDO DEPTHMAPS... (Downscale:%d  Filter:%s  MaxNeighbors:%s)" % (kwargs['DownscaleDepthMaps'], kwargs['FilterMode'], kwargs['MaxNeighbors'])
        printNovaAtividade(mensagemDepthMaps)
        # printNovaAtividade("CALCULANDO DEPTHMAPS...")
        ConstruirDepthMaps(chunk, kwargs['DownscaleDepthMaps'], kwargs['FilterMode'], kwargs['MaxNeighbors'])
        doc.save()

    if chunk.dense_cloud is None:
        mensagemDenseCloud = "CALCULANDO DENSECLOUD... (MaxNeighbors:%s)" % (kwargs['MaxNeighbors'])
        printNovaAtividade(mensagemDenseCloud)
        # printNovaAtividade("CALCULANDO NUVEM DENSA DE PONTOS...")
        ConstruirNuvemDensa(chunk, kwargs['MaxNeighbors'])
        doc.save()

    if DesejaClassificarGroundPoint and chunk.dense_cloud.meta['ClassifyGroundPoints/ram_used'] is None:
        mensagemGroundPoint = "CALCULANDO GROUNDPOINTS... (Angle:%d  Cell:%d  MaxDist:%.2f)" \
                % (kwargs['Max_Angle'], kwargs['Cell_Size'], kwargs['Max_Distance'])
        printNovaAtividade(mensagemGroundPoint)
        # printNovaAtividade("CLASSIFICANDO GROUND POINTS..." + )
        ClassificarPontosDeSolo(chunk, kwargs['Max_Angle'], kwargs['Cell_Size'], kwargs['Max_Distance'])
        printNovaAtividade("REMOVENDO PONTOS ABAIXO DO SOLO (LOW POINTS)...")
        RemoveLowPoint(chunk)
        doc.save()

    if DesejaCalcularSurface and chunk.model is None:
        printNovaAtividade("CALCULANDO MESH...")
        BuildModel(chunk)
        doc.save()

    if chunk.elevation is None:
        printNovaAtividade("CALCULANDO DSM (INCLUI CONSTRUCOES, ARVORES, ETC)")
        resolutionDSM = float(chunk.dense_cloud.meta['BuildDenseCloud/resolution']) * chunk.transform.scale * DownscaleDem
        print("DEBUG:     RESOLUCAO: %.8f" % (resolutionDSM))
        CalcularDSM(chunk, resolutionDSM)
        doc.save()

    if DesejaCriarNovoDEMSomenteComGroundPoints:
        printNovaAtividade("CALCULANDO DEM (SOMENTE GROUND POINTS)")
        # Because each chunk can only contain one elevation data Therefore, we need to duplicate
        resolutionDEM = float(chunk.dense_cloud.meta['BuildDenseCloud/resolution']) * chunk.transform.scale * DownscaleDem
        new_chunk = chunk.copy(items=[Metashape.DataSource.DepthMapsData])
        new_chunk.label = chunk.label + '_DEM'
        doc.save()
        CalcularDEM(new_chunk, resolutionDEM)
        doc.save()
        doc.chunk = chunk # Change the active chunk back

    if chunk.orthomosaic is None:
        printNovaAtividade("CONSTRUINDO A ORTOFOTO...")
        BuildMosaic(chunk, kwargs['BlendingMode'])
        doc.save()

    PastaDeExportacaoCaminhoCompleto = kwargs['PastaDeExportacaoCaminhoCompleto']
    ExportaArquivosMsg = "FIM DO PROCESSAMENTO! \nOS ITENS EXPORTADOS ESTAO NA PASTA \n%s" % \
            PastaDeExportacaoCaminhoCompleto
    NomeProjeto = os.path.splitext(os.path.split(doc.path)[1])[0]

    HouveExportacaoLas = False
    filenameLas = str(Path(PastaDeExportacaoCaminhoCompleto).joinpath(NomeProjeto + '.las'))
    if not Path(filenameLas).exists():
        printNovaAtividade("EXPORTANDO NUVEM DE PONTOS EM \n%s" % filenameLas)
        chunk.exportPoints(path=filenameLas, binary=True, save_colors=True, format=Metashape.PointsFormatLAS, crs=chunk.crs)
        HouveExportacaoLas = True
    else:
        printNovaAtividade("A NUVEM DE PONTOS NAO FOI EXPORTADA.\nJA EXISTE UM ARQUIVO %s\nAPAGUE-O E RODE ESTE SCRIPT NOVAMENTE" % filenameLas)

    HouveExportacaoTif = False
    filenameTif = str(Path(PastaDeExportacaoCaminhoCompleto).joinpath(NomeProjeto + '.tif'))
    if not Path(filenameTif).exists():
        printNovaAtividade("EXPORTANDO ORTOFOTO EM \n%s" % filenameTif)
        my_projection = Metashape.OrthoProjection()
        my_projection.crs=chunk.crs
        my_compression = Metashape.ImageCompression()
        my_compression.tiff_compression = Metashape.ImageCompression.TiffCompressionJPEG
        my_compression.jpeg_quality = 80
        my_compression.tiff_overviews = True
        chunk.exportRaster(path=filenameTif, image_format=Metashape.ImageFormat.ImageFormatTIFF, raster_transform=Metashape.RasterTransformType.RasterTransformNone, projection=my_projection, save_alpha=False, image_compression=my_compression, white_background=False, save_scheme=False, save_world=False, description="https://seusite.com.br")
        HouveExportacaoTif = True
    else:
        printNovaAtividade("A ORTOFOTO NAO FOI EXPORTADA.\nJA EXISTE UM ARQUIVO %s\nAPAGUE-O E RODE ESTE SCRIPT NOVAMENTE" % filenameTif)

    HouveExportacaoWeb = False
    ZipFileName = NomeProjeto + "_web.zip"
    filenameZip = str(Path(PastaDeExportacaoCaminhoCompleto) / ZipFileName)
    filenameTxt = filenameZip.replace('.zip', '_instrucoes_deploy.txt')
    PotreeExe = kwargs['PotreeExe']
    WwwFolder = str(Path(PastaDeExportacaoCaminhoCompleto) / "www")
    PotreeCmd = PotreeExe + " " + filenameLas + " -o " + WwwFolder + " --generate-page index"
    if not Path(filenameZip).exists():
        if Path(PotreeExe).exists():
            printNovaAtividade("GERANDO PAGINA WEB EM\n%s" % filenameZip)
            check_output(PotreeCmd, shell=True).decode() # converte o arquivo .las em aplicacao web
            zipf = zipfile.ZipFile(filenameZip, 'w', zipfile.ZIP_DEFLATED); zipdir(WwwFolder, zipf); zipf.close()
            shutil.rmtree(WwwFolder) # apaga a pasta com a aplicacao web apos a compactacao
            HouveExportacaoWeb = True
            ExportaArquivosMsg += "\n\nInstrucoes para deploy da aplicacao web:\n\n"
            ExportaArquivosMsg += "cd /var/www/html/otherapps/pointcloud\n"
            ExportaArquivosMsg += "unzip -d %s %s\n" % (NomeProjeto, ZipFileName)
            ExportaArquivosMsg += "chown -R www-data:www-data /var/www/html/otherapps/pointcloud/%s\n\n" % NomeProjeto
            ExportaArquivosMsg += "rm %s\n" % ZipFileName
            ExportaArquivosMsg += "Endereco: https://seusite.com.br/pointcloud/%s\n" % NomeProjeto
            arquivoTxt = open(filenameTxt,"w+"); arquivoTxt.write(ExportaArquivosMsg); arquivoTxt.close()
        else:
            printNovaAtividade("A APLICACAO WEB NAO FOI GERADA.\nO EXECUTAVEL %s, NAO FOI ENCONTRADO" % PotreeExe)
    else:
        printNovaAtividade("A APLICACAO WEB NAO FOI GERADA.\nJA EXISTE UM ARQUIVO %s\nAPAGUE-O E RODE ESTE SCRIPT NOVAMENTE" % filenameZip)
    # Mensagem final
    if HouveExportacaoLas or HouveExportacaoTif or HouveExportacaoWeb:
        printNovaAtividade(ExportaArquivosMsg)


def GetResolution(chunk):
    DEM_resolution = float(chunk.dense_cloud.meta['BuildDenseCloud/resolution']) * chunk.transform.scale
    DEM_resolution *= DownscaleDem
    Image_resolution = DEM_resolution / int(chunk.dense_cloud.meta['BuildDepthMaps/downscale'])
    print("RESOLUCAO DO DEM: %.8f - RESOLUCAO DA IMAGEM: %.8f" % (DEM_resolution, Image_resolution))
    return DEM_resolution, Image_resolution


def ReduceError_RU(chunk, init_threshold=15):
    printNovaAtividade("FAZ O REALINHAMENTO DAS CAMERAS ELIMINANDO INCERTEZA DE RECONSTRUCAO")
    tie_points = chunk.point_cloud
    fltr = Metashape.PointCloud.Filter()
    fltr.init(chunk, Metashape.PointCloud.Filter.ReconstructionUncertainty)
    threshold = init_threshold
    while fltr.max_value > 15:
        fltr.selectPoints(threshold)
        nselected = len([p for p in tie_points.points if p.selected])
        if nselected >= len(tie_points.points) / 2 and threshold <= 50:
            fltr.resetSelection()
            threshold += 1
            print("NOVO THRESHOLD: %d" % threshold)
            continue
        tie_points.removeSelectedPoints()
        chunk.optimizeCameras(fit_f=True, fit_cx=True, fit_cy=True, fit_b1=False, fit_b2=False,
                              fit_k1=True, fit_k2=True, fit_k3=True, fit_k4=False,
                              fit_p1=True, fit_p2=True, fit_p3=False, fit_p4=False,
                              adaptive_fitting=False, tiepoint_covariance=False)
        fltr.init(chunk, Metashape.PointCloud.Filter.ReconstructionUncertainty)
        threshold = init_threshold


def ReduceError_PA(chunk, init_threshold=2.0):
    printNovaAtividade("FAZ O REALINHAMENTO DAS CAMERAS ELIMINANDO PRECISAO DE PROJECAO")
    tie_points = chunk.point_cloud
    fltr = Metashape.PointCloud.Filter()
    fltr.init(chunk, Metashape.PointCloud.Filter.ProjectionAccuracy)
    threshold = init_threshold
    while fltr.max_value > 2.0:
        fltr.selectPoints(threshold)
        nselected = len([p for p in tie_points.points if p.selected])
        if nselected >= len(tie_points.points) / 2 and threshold <= 3.0:
            fltr.resetSelection()
            threshold += 0.1
            print("NOVO THRESHOLD: %f" % (threshold ** 2))
            continue
        tie_points.removeSelectedPoints()
        chunk.optimizeCameras(fit_f=True, fit_cx=True, fit_cy=True, fit_b1=False, fit_b2=False,
                              fit_k1=True, fit_k2=True, fit_k3=True, fit_k4=False,
                              fit_p1=True, fit_p2=True, fit_p3=False, fit_p4=False,
                              adaptive_fitting=False, tiepoint_covariance=False)
        fltr.init(chunk, Metashape.PointCloud.Filter.ProjectionAccuracy)
        threshold = init_threshold
    # This is to tighten tie point accuracy value
    chunk.tiepoint_accuracy = 0.1
    chunk.optimizeCameras(fit_f=True, fit_cx=True, fit_cy=True, fit_b1=True, fit_b2=True,
                          fit_k1=True, fit_k2=True, fit_k3=True, fit_k4=True,
                          fit_p1=True, fit_p2=True, fit_p3=True, fit_p4=True,
                          adaptive_fitting=False, tiepoint_covariance=False)


def ReduceError_RE(chunk, init_threshold=0.3):
    # This is used to reduce error based on repeojection error
    printNovaAtividade("FAZ O REALINHAMENTO DAS CAMERAS ELIMINANDO ERRO DE REPROJECAO")
    tie_points = chunk.point_cloud
    fltr = Metashape.PointCloud.Filter()
    fltr.init(chunk, Metashape.PointCloud.Filter.ReprojectionError)
    threshold = init_threshold
    while fltr.max_value > 0.3:
        fltr.selectPoints(threshold)
        nselected = len([p for p in tie_points.points if p.selected])
        if nselected >= len(tie_points.points) / 10:
            fltr.resetSelection()
            threshold += 0.01
            print("NOVO THRESHOLD: %f" % (threshold ** 2))
            continue
        tie_points.removeSelectedPoints()
        chunk.optimizeCameras(fit_f=True, fit_cx=True, fit_cy=True, fit_b1=True, fit_b2=True,
                              fit_k1=True, fit_k2=True, fit_k3=True, fit_k4=True,
                              fit_p1=True, fit_p2=True, fit_p3=True, fit_p4=True,
                              adaptive_fitting=False, tiepoint_covariance=False)
        fltr.init(chunk, Metashape.PointCloud.Filter.ReprojectionError)
        threshold = init_threshold


def HasDisabledPhotos(chunk):
    result = False
    for camera in chunk.cameras:
        if camera.enabled is False:
            result = True
            break
    return result


# As vezes o programa nao calcula do depthmap de uma ou outra imagem. isso gera o erro "null image" ao calcular a nuvem de pontos.
# Ate duas imagens com esse problema, elas sao desabilitadas e o script prossegue, mais que isso, o programa gera um erro.
def VerificarSeTodasAsFotosPossuemDepthMap(chunk):
    imagensSemDepthMap = list()
    for camera in chunk.cameras:
        if camera.enabled is True:
            if camera not in chunk.depth_maps.keys():
                imagensSemDepthMap.append(camera)
    if len(imagensSemDepthMap) > 0:
        if len(imagensSemDepthMap) <= 2:
            for camera in imagensSemDepthMap:
                camera.enabled = False # desabilita a camera
                print("A CAMERA [" + camera.label + " ] FOI DESABILITADA POR NAO TER DEPTHMAP")
        else:
            cameraList = ""
            for camera in imagensSemDepthMap:
                cameraList += camera.label + " "
            cameraList.strip() #trim
            raise RuntimeError('ESTAS FOTOS: [%s] NAO POSSUI DEPTHMAP. DESABILITE-AS E VOLTE A RODAR ESTE SCRIPT' % cameraList)
    else:
        print("TODAS AS IMAGENS POSSUEM DEPTHMAP, OK")
        # raise RuntimeError('A foto [%s] NAO POSSUI DEPTH MAP. DESABILITE-A E VOLTE A RODAR ESTE SCRIPT' % camera.label)


def RemoveDisabledPhotos(chunk):
    printNovaAtividade("REMOVE AS CAMERAS DESABILITADAS DO PROJETO E MOVE AS FOTOS PARA A PASTA \"FotosDescartadas\"")
    # print (datetime.datetime.now())
    # chunk = doc.chunk
    counter = 0
    counter_fail = 0
    counter_not_moved = 0
    counter_errors = 0
    counter_cameras = 0
    lenght = len(chunk.cameras)
    message = 'STARTING TO EVALUATE ' + str(lenght) + ' PHOTOS...'
    print (message)
    for camera in chunk.cameras:
        if camera.enabled is True:
            counter_not_moved = counter_not_moved + 1
            continue # skipping enabled cameras
        photo_path = Path(camera.photo.path)
        photo_name = str(camera.label)
        destination_dir = photo_path.parent / 'FotosDescartadas'
        destination = destination_dir / photo_path.name
        if not destination_dir.exists():
            try:
                destination_dir.mkdir()
                print ("SUCCESSFULLY CREATED THE DIRECTORY %s " % destination_dir)
            except OSError:
                print ('ERROR CREATING %s' % destination_dir)
                counter_errors = counter_errors + 1
                continue # we can't create directory - thus we can't move photo - thus we shouldn't delete it
        try:
            if photo_path.is_file():
                print ('MOVING %s ...' % photo_name)
                shutil.move(str(photo_path), str(destination))
                counter = counter + 1
                counter_cameras = counter_cameras + 1
            else:
                print ('PHOTO %s DOES NOT EXIST!' % photo_name)
                counter_cameras = counter_cameras + 1
                counter_fail = counter_fail + 1
            chunk.remove(camera)
        except OSError:
            counter_errors = counter_errors + 1
            print ('Error %s!' % photo_name)
    message_end = 'SUCCESS, ' + str(counter) + ' PHOTOS MOVED, ' + str(counter_not_moved) + ' PHOTOS NOT MOVED.\nNUMBER OF FILES UNABLE TO MOVE: ' + str(counter_fail) + '\nNUMBER OF CAMERAS REMOVED: ' + str(counter_cameras) + '\nNUMBER OF UNKNOWN ERRORRS: '+ str(counter_errors)
    print (message_end)


def RemoveLowPoint(chunk):
    chunk.dense_cloud.removePoints(Metashape.PointClass.LowPoint)


def Sirgas2000(chunk):
    if str(chunk.crs).find("SIRGAS 2000")>0 or str(chunk.crs).find("Local Coordinates")>0:
        printNovaAtividade("O SISTEMA DE REFERENCIA JA E SIRGAS 2000 ou COORDENADAS LOCAIS\nNAO HA NECESSIDADE DE CONVERTER AS COORDENADAS DO PROJETO")
    else:
        printNovaAtividade("CONVERTENDO SISTEMAS DE COORDENADAS DO PROJETO PARA SIRGAS 2000")
        out_crs = Metashape.CoordinateSystem("EPSG::31983") #sirgas2000
        for camera in chunk.cameras:
            if camera.reference.location:
                camera.reference.location = Metashape.CoordinateSystem.transform(camera.reference.location, chunk.crs, out_crs)
        for marker in chunk.markers:
            if marker.reference.location:
                marker.reference.location = Metashape.CoordinateSystem.transform(marker.reference.location, chunk.crs, out_crs)
        chunk.crs = out_crs
        chunk.updateTransform()


def zipdir(path, ziph):
    # ziph is zipfile handle https://stackoverflow.com/questions/1855095/how-to-create-a-zip-archive-of-a-directory-in-python
    for root, dirs, files in os.walk(path):
        for file in files:
            ziph.write(os.path.join(root, file), os.path.relpath(os.path.join(root, file), path))



# The following process will only be executed when running script
if __name__ == '__main__':
    # Initialising listing chunks
    chunk_list = doc.chunks
    # Loop for all enabled chunks
    for chunk in chunk_list:
        if chunk.enabled:
            Sirgas2000(chunk)
            if HasDisabledPhotos(chunk):
                # 1a execucao. Apenas remove as fotos desabilitadas.
                RemoveDisabledPhotos(chunk)
                printNovaAtividade("CAMERAS DESABILITADAS FORAM MOVIDAS PARA A PASTA \n1. RODE ESSE SCRIPT NOVAMENTE PARA ALINHAR A NUVEM DE PONTOS")
            elif chunk.point_cloud is None:
                # 2a execucao. Alinha as fotos
                AlignPhoto(chunk, DownscaleAlignment, Key_Limit, Tie_Limit, QualityFilter, QualityCriteria)
                # ReduceError_RU(chunk); ReduceError_PA(chunk); ReduceError_RE(chunk)
                printNovaAtividade("FIM DA CRIACAO DOS TIE POINTS.\n1. AJUSTE A REGION PARA O TAMANHO DESEJADO\n2. ASSOCIE OS GCPs E REALINHE AS CAMERAS\n3. SALVE O PROJETO\nEM SEGUIDA RODE ESTE SCRIPT NOVAMENTE PARA GERAR NUVEM DE PONTOS, ORTOFOTO, ETC")
            else:
                # 3a execucao. Executa o workflow (Nuvem densa, DEM, Ortofotos, etc)
                DefinirPastaDeExportacao()
                StandardWorkflow(doc, chunk, PotreeExe=PotreeExe, PastaDeExportacaoCaminhoCompleto=PastaDeExportacaoCaminhoCompleto, DownscaleDepthMaps=DownscaleDepthMaps, FilterMode=FilterMode, Max_Angle=Max_Angle, Cell_Size=Cell_Size, Max_Distance=Max_Distance, BlendingMode=BlendingMode, MaxNeighbors=MaxNeighbors)

