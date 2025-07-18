#!/usr/bin/env python3
# Copyright (c) 2018 Arista Networks, Inc.
# Use of this source code is governed by the Apache License 2.0
# that can be found in the LICENSE file.

import base64
import binascii
import importlib
import os
import sys
import tempfile
import typer
import zipfile
from cryptography import x509, exceptions as CryptoException
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, rsa, utils, padding
from pathlib import Path
from typing import Annotated, Optional

from switools import signaturelib
from switools.callbacks import _path_exists_callback

ROOT_CA_FILE_NAME = 'ARISTA_ROOT_CA.crt'
ROOT_CA = importlib.resources.files(__name__).joinpath(ROOT_CA_FILE_NAME).read_bytes()

class SwiSignature:
   def __init__( self ):
      self.version = ""
      self.hashAlgo = ""
      self.cert = ""
      self.signature = ""
      self.offset = 0
      self.size = 0

   def updateFields( self, sigFile ):
      """ Update the fields of this SwiSignature with the file object
      @sigFile, a new-line-delimited file with key-value
      pairs in the form of key:value. For example:
      key1:value1
      key2:value2
      etc. """
      for line in sigFile:
         data = line.decode( "utf-8", "backslashreplace" ).split( ':' )
         if ( len( data ) == 2 ):
            if data[ 0 ] == 'Version':
               self.version = data[ 1 ].strip()
            elif data[ 0 ] == 'HashAlgorithm':
               self.hashAlgo = data[ 1 ].strip()
            elif data[ 0 ] == 'IssuerCert':
               self.cert = base64Decode( data[ 1 ].strip() )
            elif data[ 0 ] == 'Signature':
               self.signature = base64Decode( data [ 1 ].strip() )
            elif data[ 0 ] == 'CRCPadding': # last entry
               # has binary content that sometimes contains \n, so break before err
               break
         else:
            if not data[ 0 ] == 'CRCPadding': # padding value is binary, may have :
               print( 'Unexpected format for line in swi[x]-signature file: %s' % line )

class VERIFY_SWI_RESULT:
   SUCCESS = 0
   ERROR = -1
   ERROR_SIGNATURE_FILE = 3
   ERROR_VERIFICATION = 4
   ERROR_HASH_ALGORITHM = 5
   ERROR_SIGNATURE_FORMAT = 6
   ERROR_NOT_A_SWI = 7
   ERROR_CERT_MISMATCH = 8
   ERROR_INVALID_SIGNING_CERT = 9
   ERROR_INVALID_ROOT_CERT = 10
   ERROR_NO_SWADAPT_UTIL = 11
   ERROR_SWADAPT_FAILURE = 12

VERIFY_SWI_MESSAGE = {
   VERIFY_SWI_RESULT.SUCCESS: "SWI/X verification successful.",
   VERIFY_SWI_RESULT.ERROR_SIGNATURE_FILE: "SWI/X is not signed." ,
   VERIFY_SWI_RESULT.ERROR_VERIFICATION: "SWI/X verification failed.",
   VERIFY_SWI_RESULT.ERROR_HASH_ALGORITHM: "Unsupported hash algorithm for SWI/X" \
                                           " verification.",
   VERIFY_SWI_RESULT.ERROR_SIGNATURE_FORMAT: "Invalid SWI/X signature file.",
   VERIFY_SWI_RESULT.ERROR_NOT_A_SWI: "Input does not seem to be a swi/x image.",
   VERIFY_SWI_RESULT.ERROR_CERT_MISMATCH: "Signing certificate used to sign SWI/X" \
                                          " is not signed by root certificate.",
   VERIFY_SWI_RESULT.ERROR_INVALID_SIGNING_CERT: "Signing certificate is not a" \
                                                 " valid certificate.",
   VERIFY_SWI_RESULT.ERROR_INVALID_ROOT_CERT: "Root certificate is not a" \
                                              " valid certificate.",
   VERIFY_SWI_RESULT.ERROR_NO_SWADAPT_UTIL: "Utility 'swadapt' not found in image.",
   VERIFY_SWI_RESULT.ERROR_SWADAPT_FAILURE: "Utility 'swadapt' failed to extract sub-image.",
}

def printStatus( code ):
   if code == VERIFY_SWI_RESULT.SUCCESS:
      print( VERIFY_SWI_MESSAGE[ code ] )
   else:
      print( VERIFY_SWI_MESSAGE[ code ], file=sys.stderr )

class X509CertException( Exception ):
   def __init__( self, code ):
      self.code = code
      message = VERIFY_SWI_MESSAGE[ code ]
      super( X509CertException, self ).__init__( message )

def getSwiSignatureData( swiFile ):
   try:
      swiSignature = SwiSignature()
      with zipfile.ZipFile( swiFile, 'r' ) as swi:
         sigInfo = swi.getinfo( signaturelib.getSigFileName( swiFile ) )
         with swi.open( sigInfo, 'r' ) as sigFile:
            # Get offset from our current location before processing sigFile
            swiSignature.offset = sigFile._fileobj.tell()
            swiSignature.size = sigInfo.compress_size
            swiSignature.updateFields( sigFile )
            return swiSignature
   except KeyError:
      # Occurs if SIG_FILE_NAME is not in the swi (the SWI is not
      # signed properly)
      return None

def verifySignatureFormat( swiSignature ):
   # Check that the signing cert, hash algorithm, and signatures are valid
   return ( len( swiSignature.cert ) != 0 and
            len( swiSignature.hashAlgo ) != 0 and
            len( swiSignature.signature ) != 0 )

def base64Decode( text ):
   try:
      return base64.standard_b64decode( text )
   except ( binascii.Error, TypeError ):
      return ""

def loadSigningCert( swiSignature ):
   # Read signing cert from memory and load it as an X509 cert object
   try:
      return x509.load_pem_x509_certificate( swiSignature.cert )
   except ValueError:
      raise X509CertException( VERIFY_SWI_RESULT.ERROR_INVALID_SIGNING_CERT )

def signingCertValid( signingCertX509, rootCAFile ):
   # Validate cert used to sign SWI with root CA
   # Note: we cannot directly use verify_directly_issued_by. If root.subject
   # and signing.issuer are not the same, but verify works, the function must
   # return VERIFY_SWI_RESULT.SUCCESS. With verify_directly_issued_by it would
   # return an error
   try:
      rootCa = x509.load_pem_x509_certificate( ROOT_CA )
      if rootCAFile != ROOT_CA_FILE_NAME:
         with open(rootCAFile, "rb") as pems:
            rootCa = x509.load_pem_x509_certificate( pems.read() )
   except Exception:
      raise X509CertException( VERIFY_SWI_RESULT.ERROR_INVALID_ROOT_CERT )
   try:
      rootKey = rootCa.public_key()
      if isinstance( rootKey, ec.EllipticCurvePublicKey ):
         rootKey.verify(
            signingCertX509.signature,
            signingCertX509.tbs_certificate_bytes,
            signingCertX509.signature_algorithm_parameters
         )
      elif isinstance( rootKey, rsa.RSAPublicKey ):
         rootKey.verify(
            signingCertX509.signature,
            signingCertX509.tbs_certificate_bytes,
            signingCertX509.signature_algorithm_parameters,
            signingCertX509.signature_hash_algorithm
         )
      else:
         signingCertX509.verify_directly_issued_by( rootCa )
      return VERIFY_SWI_RESULT.SUCCESS
   except ( ValueError, TypeError, CryptoException.InvalidSignature,
            CryptoException.UnsupportedAlgorithm ):
      return VERIFY_SWI_RESULT.ERROR_CERT_MISMATCH

def getHashAlgo( swiSignature ):
   hashAlgo = swiSignature.hashAlgo
   # For now, we always use SHA-256
   if hashAlgo in ["SHA-256", "sha256"]:
      return hashes.SHA256()
   return None

def swiSignatureValid( swiFile, swiSignature, signingCertX509 ):
   hashAlgo = getHashAlgo( swiSignature )
   if hashAlgo is None:
      return VERIFY_SWI_RESULT.ERROR_HASH_ALGORITHM

   # Verify the swi against the signature in swi-signature
   offset = 0
   BLOCK_SIZE = 65536
   pubkey = signingCertX509.public_key()
   hasher = hashes.Hash( hashAlgo )
   # Read the swi file into the verification function, up to the swi signature file
   with open( swiFile, 'rb' ) as swi:
      while offset < swiSignature.offset:
         if offset + BLOCK_SIZE < swiSignature.offset:
            numBytes = BLOCK_SIZE
         else:
            numBytes = swiSignature.offset - offset
         hasher.update( swi.read( numBytes ) )
         offset += numBytes
      # Now that we're at the swi-signature file, read zero's into the verification
      # function up to the size of the swi-signature file.
      hasher.update( b'\000' * swiSignature.size )

      # Now jump to the end of the swi-signature file and read the rest of the swi
      # file into the verification function
      swi.seek( swiSignature.size, os.SEEK_CUR )
      for block in iter( lambda: swi.read( BLOCK_SIZE ), b'' ):
         hasher.update( block )
   # After reading the swi file and skipping over the swi signature, check that the
   # data signed with pubkey is the same as signature in the swi-signature.
   try:
      if isinstance( pubkey, ec.EllipticCurvePublicKey ):
         pubkey.verify(
            signature=swiSignature.signature,
            data=hasher.finalize(),
            signature_algorithm=ec.ECDSA( utils.Prehashed( hashAlgo ) )
         )
         return VERIFY_SWI_RESULT.SUCCESS
      elif isinstance( pubkey, rsa.RSAPublicKey ):
         pubkey.verify(
            signature=swiSignature.signature,
            data=hasher.finalize(),
            padding=padding.PKCS1v15(),
            algorithm=utils.Prehashed( hashAlgo )
         )
         return VERIFY_SWI_RESULT.SUCCESS
      else:
         return VERIFY_SWI_RESULT.ERROR_VERIFICATION
   except CryptoException.InvalidSignature:
      return VERIFY_SWI_RESULT.ERROR_VERIFICATION

def verifySwi( swi, rootCA=ROOT_CA_FILE_NAME ):
   try:
      if not zipfile.is_zipfile( swi ):
         return VERIFY_SWI_RESULT.ERROR_NOT_A_SWI
      swiSignature = getSwiSignatureData( swi )
      if swiSignature is None:
         return VERIFY_SWI_RESULT.ERROR_SIGNATURE_FILE
      if not verifySignatureFormat( swiSignature ):
         return VERIFY_SWI_RESULT.ERROR_SIGNATURE_FORMAT
      signingCert = loadSigningCert( swiSignature )
      result = signingCertValid( signingCert, rootCA )
      if result != VERIFY_SWI_RESULT.SUCCESS:
         # Signing cert invalid
         return result
      result = swiSignatureValid( swi, swiSignature, signingCert )
      if result != VERIFY_SWI_RESULT.SUCCESS:
         return result
      else:
         return VERIFY_SWI_RESULT.SUCCESS
   except IOError as e:
      print( e )
      return VERIFY_SWI_RESULT.ERROR_VERIFICATION
   except X509CertException as e:
      return e.code

def verifyAllSwi( workDir, swi, rootCA=ROOT_CA_FILE_NAME ):
   # We will extract sub-images to /tmp to verify their signatures
   subImageError = False

   # Make sure the image we got is a swi file
   if not signaturelib.checkIsSwiFile( swi ):
      print( "Error: '%s' does not look like an EOS image" % swi )
      return VERIFY_SWI_RESULT.ERROR_NOT_A_SWI
   optims = signaturelib.getOptimizations( swi, workDir )
   # If image has multiple sub-images, verify each (don't stop at failure, print
   # each error and return a generic error code) and finally check container image
   if not ( optims is None or len( optims ) == 1 or "DEFAULT" in optims ):
      print( "Optimizations in %s: %s" % ( swi, " ".join( optims ) ) )
      if not signaturelib.extractSwadapt( swi, workDir ):
         retCode = VERIFY_SWI_RESULT.ERROR_NO_SWADAPT_UTIL
         printStatus( retCode )
         return retCode
      for optim in optims:
         optimImage = "%s/%s.swi" % ( workDir, optim )
         # extract sub-image
         if not signaturelib.adaptSwi( swi, optimImage, optim, workDir ):
            retCode = VERIFY_SWI_RESULT.ERROR_SWADAPT_FAILURE
            printStatus( retCode )
            return retCode
         retCode = verifySwi( optimImage, rootCA )
         os.remove( optimImage )
         print( "%s: %s" % ( optim, VERIFY_SWI_MESSAGE[ retCode ] ) )
         if retCode != VERIFY_SWI_RESULT.SUCCESS:
            subImageError = True
   # Finally check the container image (or legacy "one level" image)
   retCode = verifySwi( swi, rootCA )
   printStatus( retCode )
   if subImageError:
      retCode = VERIFY_SWI_RESULT.ERROR

   return retCode

app = typer.Typer(add_completion=False)

@app.command(name="verify")
def _verify(
   swiFile: Annotated[Path, typer.Argument(help="SWI/X file to verify.", callback=_path_exists_callback)],
   caFile: Annotated[Optional[Path], typer.Option("--CAfile", help="Root certificate to verify against.", callback=_path_exists_callback)] = None,
):
   """
   Verify Arista SWI image or SWIX extension.
   """
   with tempfile.TemporaryDirectory( prefix="swi-verify-" ) as work_dir:
      verifyAllSwi( work_dir, swiFile, caFile )

if __name__ == "__main__":
   app()